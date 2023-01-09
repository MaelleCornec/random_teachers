
import argparse
import copy
import json
import os
import sys
from math import sqrt
from time import strftime, sleep
from typing import Dict, Tuple

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


def load_dino(identifier:str, config:Configuration) -> DINO:
    ckpt_path, name = identifier.split(':')

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
    
    # init if required by .init() suffix
    if name.endswith('.init()'):
        student, teacher = init_student_teacher(config, student)
        dino.student = student
        dino.teacher = teacher

    return dino

def load_model(identifier:str, config:Configuration) -> DINOModel:
    dino = load_dino(identifier, config)
    ckpt_path, name = identifier.split(':')

    if name.startswith('teacher'):
        return dino.teacher
    
    if name.startswith('student'):
        return dino.student

    raise ValueError(f'Unkown name \'{name}\', should be either \'teacher\' or \'student\'.')


class ParamProjector():
    def __init__(self, vec0:torch.Tensor, vec1:torch.Tensor, vec2:torch.Tensor, center=None, scale=None) -> None:
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

        self.affine_inv = torch.linalg.lstsq(self.basis, self.affine).solution
        if self.scale in {'l2_ortho', 'rms_ortho'}:
            self.basis:torch.Tensor = torch.linalg.svd(self.basis, full_matrices=False).U
            self.affine_inv = self.basis.T @ self.affine

        #self.affine_inv = torch.zeros_like(self.affine_inv)

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
        
    def __call__(self, inp:torch.Tensor, is_direction=False) -> torch.Tensor:
        if inp.shape[0] == self.dim:
            return self.project(inp, is_direction)
        if inp.shape[0] == 2:
            return self.map(inp, is_direction)
        raise ValueError('Cannot infer whether to project or map input.')


def eval_student(student:DINOModel, teacher:DINOModel, criterion, prober:Prober, train_dl:DataLoader, valid_dl:DataLoader, device=None):
    out = {}

    # process training set
    train_data = []
    loss, numel = 0, 0
    for batch in train_dl:
        batch = batch[0].to(device), batch[1].to(device) if device else batch
        model_out = student(batch[0])
        teacher_out = teacher(batch[0])
        
        # gather loss
        numel += batch[1].shape[0]
        loss += criterion(model_out['logits'], teacher_out['logits']).sum()
        train_data.append((model_out['embeddings'].squeeze(), batch[1]))
    out['train/loss'] = loss / numel # full dataset loss

    # process validation set
    valid_data = []
    loss, numel = 0, 0
    for batch in valid_dl:
        batch = batch[0].to(device), batch[1].to(device) if device else batch
        
        # run an isolated validation 
        model_out = student(batch[0])
        teacher_out = teacher(batch[0])
        
        # gather loss
        numel += batch[1].shape[0]
        loss += criterion(model_out['logits'], teacher_out['logits']).sum()
        valid_data.append((model_out['embeddings'].squeeze(), batch[1]))
    out['valid/loss'] = loss / numel

    # analyze probes
    probe = prober.eval_probe(train_data, valid_data, device=device)
    for key, val in probe.items():
        out[f'probe/{key}'] = val

    normalize_data(train_data, valid_data)
    probe = prober.eval_probe(train_data, valid_data, device=device)
    for key, val in probe.items():
        out[f'probe/norm/{key}'] = val

    return out

def mse_criterion(pred, targ):
    return U.mean_squared_error(pred, targ)

def ce_criterion(pred, targ):
    return U.cross_entropy(F.log_softmax(pred, dim=1), F.softmax(targ, dim=-1))


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

    # DINO and Data Setup.
    train_dl, valid_dl = load_data(config, args['batchsize'], args['num_workers'], not args['force_cpu'])
    criterion = mse_criterion if args['loss']=='MSE' else ce_criterion

    # Probing Setup
    analyses = {}
    if args['probing_epochs'] > 0:
        analyses['lin'] = LinearAnalysis(args['probing_epochs'])
    if args['probing_k'] > 0:
        analyses['knn'] = KNNAnalysis(args['probing_k'])
    prober = Prober(encoders={}, analyses=analyses, train_dl=None, valid_dl=None, n_classes=train_dl.dataset.ds_classes)

    out_list = []
    student = copy.deepcopy(teacher)
    for idx, coord in enumerate(tqdm(coords)):
        # get vector and model from coordinate
        vec = P(coord)
        U.vector_to_module(vec, student)

        # make experiments and store results
        out = eval_student(student, teacher, criterion, prober, train_dl, valid_dl, device)
        out['coord'] = coord
        out['l2norm'] = vec.norm(p=2)

        # return tensor on cpu
        out_list.append({k: torch.tensor(v).cpu() for k,v in out.items()})
        print(idx, file=sys.stderr, end='\r', flush=True)

    return out_list 


def main(args):
    # Make grid/coords with matrix indexing -> grid[y,x] = (p_x,p_y)
    X = torch.arange(args['xmin'], args['xmax'] + args['stepsize'], args['stepsize'])
    Y = torch.arange(args['ymin'], args['ymax'] + args['stepsize'], args['stepsize'])
    grid = torch.stack(torch.meshgrid(X, Y, indexing='ij'), dim=-1) # shape = (len(X), len(Y), 2)
    coords = grid.reshape((-1, 2)) # list of coordinates of shape 2
    coords = torch.tensor_split(coords, args['num_jobs']) # split into njobs chunks

    print(f'Evaluating {len(X)*len(Y)} coordinates in {len(coords)} jobs of size ~{len(coords[0])}..')
    #if input().lower() not in {'y', 'yes'}:
    #    sys.exit()

    # Start executor
    executor = submitit.AutoExecutor(folder=logdir, cluster=args['cluster'])
    executor.update_parameters(
            slurm_cpus_per_task=args['num_workers'],
            slurm_mem_per_cpu=4096,
            slurm_gpus_per_node=1,
            #slurm_time=4,
        )

    jobs = executor.map_array(eval_coords, coords, len(coords) * [args])

    # track progress as printed in stderr
    with tqdm(total=len(X)*len(Y)) as pbar:
        while any([int(job.state == 'RUNNING') for job in jobs]):
            pbar.update(sum([int(job.paths.stderr) for job in jobs]))
            sleep(1)

    # gather results into tensors of shape (len(X)*len(Y), -1)
    out:Dict[str, torch.Tensor] = {}
    for idx, job in enumerate(jobs):
        for sub_idx, res in enumerate(job.results()[0]):
            for key, val in res.items():
                if key not in out.keys():
                    out[key] = torch.zeros((len(X)*len(Y), *val.shape))
                out[key][idx+sub_idx] = val

    # Reshape results for matrix indexing (len(X), len(Y), -1) and save
    for key, val in out.items():
        val = val.reshape((len(X), len(Y), -1))
        fname = os.path.join(dir, f"{key.replace('/', '_')}.pt")
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
    parser.add_argument('--loss', choices={'MSE', 'CE'})
    parser.add_argument('--xmin', type=float)
    parser.add_argument('--xmax', type=float)
    parser.add_argument('--ymin', type=float)
    parser.add_argument('--ymax', type=float)
    parser.add_argument('--stepsize', type=float)

    # Probing arguments
    parser.add_argument('--probing_epochs', type=int, default=10)
    parser.add_argument('--probing_k', type=int, default=20)

    # General arguments
    parser.add_argument('--runname', type=str, default=strftime('%Y-%m-%d--%H-%M'))
    parser.add_argument('--num_jobs', type=int, default=1)
    parser.add_argument('--num_workers', type=int, default=4)
    parser.add_argument('--batchsize', type=int, default=512)
    parser.add_argument('--cluster', choices={None, 'local', 'debug'}, default=None)
    parser.add_argument('--force_cpu', action='store_true')
    args = vars(parser.parse_args())

    # Prepare directories and store 
    dir = os.path.join(os.environ['DINO_RESULTS'], 'paramplane', args['runname'])
    logdir = os.path.join(dir, 'logs')
    os.makedirs(logdir, exist_ok=True)

    # Store args to directory
    with open(os.path.join(dir, 'args.json'), 'w') as f:
            s = json.dumps(args, indent=2)
            f.write(s)

    main(args)
