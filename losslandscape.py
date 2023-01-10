
import argparse
import copy
import json
import os
import sys
from math import sqrt
from time import strftime, sleep
from typing import Dict, Tuple
import re
from collections import deque

import submitit
import torch
from tqdm import tqdm

import dinopl.utils as U
from configuration import (Configuration, create_mc_spec, get_encoder,
                           init_student_teacher, get_dataset)
from dinopl import DINO, DINOHead, DINOModel
from dinopl.augmentation import MultiCrop

from dinopl.probing import KNNAnalysis, LinearAnalysis, Prober, normalize_data
from torch.utils.data import DataLoader
from torchvision import transforms
from torch.nn import functional as F


class ParamProjector():
    def __init__(self, vec0:torch.Tensor, vec1:torch.Tensor, vec2:torch.Tensor, center=None, scale=None) -> None:
        if vec0.dim()>1 or vec1.dim()>1 or vec2.dim()>1:
            raise ValueError(f'Vectors need to be 1D, but are of dims {vec0.dim()}, {vec0.dim()}, {vec0.dim()}.')
        if center not in {None, '', 'mean', 'minnorm'}:
            raise ValueError(f'Unkown option \'{center}\' for argument \'center\'.')
        if scale not in {None, '', 'l2_ortho', 'rms_ortho'}:
            raise ValueError(f'Unkown option \'{scale}\' for argument \'scale\'.')
        self.center = center
        self.scale = scale
        self.dim = vec0.numel()

        self.affine:torch.Tensor = vec0
        if self.center == 'mean':
            self.affine = (vec0 + vec1 + vec2) / 3

        self.basis = torch.stack([vec1, vec2], dim=1)
        self.basis = self.basis - self.affine.unsqueeze(1)

        if self.center == 'minnorm':
            offset = self.basis @ torch.linalg.lstsq(self.basis, -self.affine).solution # origin projected to plane relative to affine
            self.affine = self.affine + offset
            self.basis = self.basis - offset.unsqueeze(1)

        if self.scale in {'l2_ortho', 'rms_ortho'}:
            self.basis:torch.Tensor = torch.linalg.svd(self.basis, full_matrices=False).U

    def project(self, vec:torch.Tensor, is_position=True) -> torch.Tensor:
        if is_position:
            vec = vec - self.affine

        if self.scale in {'l2_ortho', 'rms_ortho'}:
            coord = self.basis.T @ vec
        else:
            coord = torch.linalg.lstsq(self.basis, vec).solution

        if self.scale == 'rms_ortho': # rescale to preserve rms instead of norm
            coord = coord / sqrt(self.dim) * sqrt(2)
        return coord

    def map(self, coord:torch.Tensor, is_position=True) -> torch.Tensor:
        if self.scale == 'rms_ortho': # rescale to preserve rms instead of norm
            coord = coord / sqrt(2) * sqrt(self.dim)

        vec = self.basis @ coord
        
        if is_position:
            vec = vec + self.affine
        return vec

    def error(self, vec:torch.Tensor, p=2):
        diff = vec - self.map(self.project(vec))
        p = float(p) if p=='inf' else p
        return diff.square().mean().sqrt() if p=='rms' else diff.norm(p=p)
        
    def __call__(self, inp:torch.Tensor, is_position=True) -> torch.Tensor:
        if inp.shape[0] == self.dim:
            return self.project(inp, is_position)
        if inp.shape[0] == 2:
            return self.map(inp, is_position)
        raise ValueError('Cannot infer whether to project or map input.')


def load_data(config, batchsize, num_workers, pin_memory) -> Tuple[DataLoader, DataLoader]:
    DSet = get_dataset(config)
    trfm = transforms.Compose([ # self-training
                    transforms.Lambda(lambda img: img.convert('RGB')), transforms.ToTensor(),
                    transforms.Normalize(DSet.mean, DSet.std),
                ])
    #trfm = MultiCrop(config.mc_spec, per_crop_transform=trfm)

    train_ds = DSet(root=os.environ['DINO_DATA'], train=True, transform=trfm, download=False)
    valid_ds = DSet(root=os.environ['DINO_DATA'], train=False, transform=trfm, download=False)
    train_dl = DataLoader(dataset=train_ds, batch_size=batchsize, num_workers=num_workers, pin_memory=pin_memory)
    valid_dl = DataLoader(dataset=valid_ds, batch_size=batchsize, num_workers=num_workers, pin_memory=pin_memory)
    return train_dl, valid_dl

def load_model(identifier:str, config:Configuration=None) -> DINOModel:

    ckpt_path, name = identifier.split(':')
    if config is None:
        config =  Configuration.from_json(os.path.join(os.path.dirname(ckpt_path), 'config.json'))
        config.mc_spec = create_mc_spec(config)

    # get configuration and prepare model
    enc = get_encoder(config)()
    config.embed_dim = enc.embed_dim
    head = DINOHead(config.embed_dim, config.out_dim, 
        hidden_dims=config.hid_dims, 
        l2bot_dim=config.l2bot_dim, 
        l2bot_cfg=config.l2bot_cfg,
        use_bn=config.mlp_bn,
        act_fn=config.mlp_act)
    student = DINOModel(enc, head)
    teacher = copy.deepcopy(student)

    # load DINO checkpoint
    dino = DINO.load_from_checkpoint(ckpt_path, map_location='cpu', mc_spec=config.mc_spec, student=student, teacher=teacher)
    
    # init if required by .init suffix
    if name.endswith('.init'):
        student, teacher = init_student_teacher(config, student)
        dino.student = student
        dino.teacher = teacher

    if name.startswith('teacher'):
        return dino.teacher
    
    if name.startswith('student'):
        return dino.student

    raise ValueError(f'Unkown name \'{name}\', should be either \'teacher\' or \'student\'.')


def update_losses(student_out, teacher_out, losses):
    preds, targs = student_out['logits'], teacher_out['logits']
    losses['MSE'] += U.mean_squared_error(preds, targs).sum()

    log_preds, targs = F.log_softmax(student_out['logits'], dim=-1), F.softmax(teacher_out['logits'], dim=-1)
    losses['CE'] += U.cross_entropy(log_preds, targs).sum()
    losses['KL'] += U.cross_entropy(log_preds, targs).sum()
    losses['H'] += U.entropy(log_preds.exp(), log_preds).sum()


def eval_student(student:DINOModel, teacher:DINOModel, prober:Prober, train_dl:DataLoader, valid_dl:DataLoader, device=None):
    out = {}

    # Process training set
    numel = 0
    train_data = []
    losses = {'MSE':0, 'CE':0, 'KL':0, 'H':0}
    for batch in train_dl:
        batch = batch[0].to(device), batch[1].to(device) if device else batch
        
        # compute and update losses
        teacher_out = teacher(batch[0])
        student_out = student(batch[0])
        update_losses(student_out, teacher_out, losses)

        numel += batch[1].shape[0]
        train_data.append((student_out['embeddings'].squeeze(), batch[1]))

    for loss_name, loss_value in losses.items():
        out[f'train/{loss_name}'] = loss_value / numel # average


    # Process validation set
    numel = 0
    valid_data = []
    losses = {'MSE':0, 'CE':0, 'KL':0, 'H':0}
    for batch in valid_dl:
        batch = batch[0].to(device), batch[1].to(device) if device else batch
        
        # compute and update losses
        teacher_out = teacher(batch[0])
        student_out = student(batch[0])
        update_losses(student_out, teacher_out, losses)
        
        # gather loss
        numel += batch[1].shape[0]
        valid_data.append((student_out['embeddings'].squeeze(), batch[1]))

    for loss_name, loss_value in losses.items():
        out[f'valid/{loss_name}'] = loss_value / numel # average


    # Analyze probes
    probe = prober.eval_probe(train_data, valid_data, device=device)
    for key, val in probe.items():
        out[f'probe/{key}'] = torch.tensor(val)

    normalize_data(train_data, valid_data)
    probe = prober.eval_probe(train_data, valid_data, device=device)
    for key, val in probe.items():
        out[f'probe/norm/{key}'] = torch.tensor(val)

    return out


def eval_coords(coords:torch.Tensor, args):
    device = torch.device('cpu' if args['force_cpu'] else U.pick_single_gpu())
    coords = coords.to(device)

    config =  Configuration.from_json(os.path.join(os.path.dirname(args['vec0']), 'config.json'))
    config.mc_spec = create_mc_spec(config)

    # Setup ParamProjector.
    teacher = load_model(args['vec0'], config).to(device=device)
    model1 = load_model(args['vec1'], config).to(device=device)
    model2 = load_model(args['vec2'], config).to(device=device)
    
    P = ParamProjector(
        vec0=U.module_to_vector(teacher),
        vec1=U.module_to_vector(model1),
        vec2=U.module_to_vector(model2),
        center=args['projector_center'],
        scale=args['projector_scale']
    )

    print(f'Norm of origin is {P(torch.zeros_like(coords[0])).norm():.3f}')
    print(f'Norm of {args["vec0"]} is {U.module_to_vector(teacher).norm():.3f}')
    print(f'Norm of {args["vec1"]} is {U.module_to_vector(model1).norm():.3f}')
    print(f'Norm of {args["vec2"]} is {U.module_to_vector(model2).norm():.3f}')

    # DINO and Data Setup.
    train_dl, valid_dl = load_data(config, args['batchsize'], args['num_workers'], not args['force_cpu'])

    # Probing Setup
    analyses = {}
    if args['probing_epochs'] > 0:
        analyses['lin'] = LinearAnalysis(args['probing_epochs'])
    if args['probing_k'] > 0:
        analyses['knn'] = KNNAnalysis(args['probing_k'])
    prober = Prober(encoders={}, analyses=analyses, train_dl=None, valid_dl=None, 
                    n_classes=train_dl.dataset.ds_classes, seed=args['prober_seed'])

    out_list = []
    student = copy.deepcopy(teacher)
    for coord in tqdm(coords, postfix='unique postfix'): # add postfix to make unique for parsing
        # get vector and model from coordinate
        vec = P(coord)
        U.vector_to_module(vec, student)

        # make experiments and store results
        out = eval_student(student, teacher, prober, train_dl, valid_dl, device)
        out['coord'] = coord
        out['l2norm'] = vec.norm(p=2)

        # return tensor on cpu
        out_list.append({k: v.cpu() for k,v in out.items()})

    return out_list 


def parse_tqdm_state(fname):
    try:
        with open(fname) as f:
            lastline = deque(f, 1).pop()
        return int(re.findall(r'(?<=\|\s)(\d+)(?=/\d+\s\[.*unique\spostfix)', lastline)[-1])
    except:
        return 0

def main(args):
    # Make grid/coords with matrix indexing -> grid[y,x] = (p_x,p_y)
    X = torch.arange(args['xmin'], args['xmax'] + args['stepsize'], args['stepsize'])
    Y = torch.arange(args['ymin'], args['ymax'] + args['stepsize'], args['stepsize'])
    grid = torch.stack(torch.meshgrid(X, Y, indexing='ij'), dim=-1) # shape = (len(X), len(Y), 2)
    coords = grid.reshape((-1, 2)) # list of coordinates of shape 2
    coords = torch.tensor_split(coords, args['num_jobs']) # split into njobs chunks
    coords = [c for c in coords if c.nelement() > 0] # discard empty tensors

    print(f'Evaluating {len(X)*len(Y)} coordinates in {len(coords)} jobs of size ~{len(coords[0])}..')

    # Start executor
    executor = submitit.AutoExecutor(folder=args['logdir'], cluster=args['cluster'])
    executor.update_parameters(
            slurm_cpus_per_task=args['num_workers'],
            slurm_mem_per_cpu=args['mem_per_cpu'],
            slurm_time=args['time'],
            slurm_gpus_per_node=args['gpus'],
        )

    jobs = executor.map_array(eval_coords, coords, len(coords) * [args])

    # Track progress as printed in stderr
    with tqdm(total=len(X)*len(Y), smoothing=0) as pbar:
        while any([not job.done() for job in jobs]):
            pbar.n = max(pbar.n, sum([parse_tqdm_state(job.paths.stderr) for job in jobs]))
            pbar.set_postfix({'#jobs':sum([job.state=='RUNNING' for job in jobs])})
            pbar.update(0)
            sleep(1)
            
        pbar.n = sum([parse_tqdm_state(job.paths.stderr) for job in jobs])
        pbar.update(0) # set to done

    # Gather results into lists of len len(X)*len(Y)
    out:Dict[str, torch.Tensor] = {}
    for job in jobs:
        for res in job.results()[0]: # no subtasks
            for key, val in res.items():
                if key not in out.keys():
                    out[key] = []
                out[key].append(val)

    # Gather lists into matrix-indexed tensors of shape (len(X), len(Y), -1) and save
    for key, val in out.items():
        val = torch.stack(val, dim=0).reshape((len(X), len(Y), -1)).squeeze()
        fname = os.path.join(args['dir'], f"{key.replace('/', '_')}.pt")
        print(f'Saving {fname} of shape {val.shape}')
        torch.save(val, fname)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    # Projector arguments
    parser.add_argument('vec0', type=str) # results/DINO/22abl14o/last.ckpt:teacher   # alpha=0
    parser.add_argument('vec1', type=str) # results/DINO/22abl14o/last.ckpt:student   # alpha=0
    parser.add_argument('vec2', type=str) # results/DINO/3mtlpc13/last.ckpt:student   # alpha=1
    parser.add_argument('--projector_center', choices={'', 'mean', 'minnorm'}, default='minnorm')
    parser.add_argument('--projector_scale', choices={'', 'l2_ortho', 'rms_ortho'}, default='l2_ortho')

    # Image properties
    parser.add_argument('--xmin', type=float)
    parser.add_argument('--xmax', type=float)
    parser.add_argument('--ymin', type=float)
    parser.add_argument('--ymax', type=float)
    parser.add_argument('--stepsize', type=float)

    # Probing arguments
    parser.add_argument('--batchsize', type=int, default=512)
    parser.add_argument('--probing_epochs', type=int, default=10)
    parser.add_argument('--probing_k', type=int, default=20)
    parser.add_argument('--prober_seed', type=int, default=1234567890)

    # General arguments
    parser.add_argument('--runname', type=str, default=strftime('%Y-%m-%d--%H-%M'))
    parser.add_argument('--cluster', choices={None, 'local', 'debug'}, default=None)
    parser.add_argument('--num_jobs', type=int, default=1)
    parser.add_argument('--gpus', type=str, default='1')
    parser.add_argument('--num_workers', type=int, default=4)
    parser.add_argument('--mem_per_cpu', type=int, default=4096)
    parser.add_argument('--time', type=str, default='04:00:00')
    parser.add_argument('--force_cpu', action='store_true')
    args = vars(parser.parse_args())

    # Prepare directories for logging and storing
    args['dir'] = os.path.join(os.environ['DINO_RESULTS'], 'losslandscape', args['runname'])
    args['logdir'] = os.path.join(args['dir'], 'logs')
    os.makedirs(args['dir'], exist_ok=True)
    print(f'Logging to {args["logdir"]}')

    # Make paths relative and machine independent for saving
    args_for_saving = copy.deepcopy(args)
    args_for_saving['vec0'] = os.path.relpath(args['vec0'], os.environ['DINO_RESULTS'])
    args_for_saving['vec1'] = os.path.relpath(args['vec1'], os.environ['DINO_RESULTS'])
    args_for_saving['vec2'] = os.path.relpath(args['vec2'], os.environ['DINO_RESULTS'])

    # Store args to directory
    with open(os.path.join(args['dir'], 'args.json'), 'w') as f:
            s = json.dumps(args_for_saving, indent=2)
            f.write(s)

    main(args)
