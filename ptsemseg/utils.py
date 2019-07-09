"""
Misc Utility functions
"""
import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt

import itertools
import torch
import torch.nn.functional as F
import copy
import math
import tqdm
import os
import logging
import datetime
import numpy as np

from collections import OrderedDict

def recursive_glob(rootdir=".", suffix=""):
    """Performs recursive glob with given suffix and rootdir 
        :param rootdir is the root directory
        :param suffix is the suffix to be searched
    """
    return [
        os.path.join(looproot, filename)
        for looproot, _, filenames in os.walk(rootdir)
        for filename in filenames
        if filename.endswith(suffix)
    ]


def alpha_blend(input_image, segmentation_mask, alpha=0.5):
    """Alpha Blending utility to overlay RGB masks on RBG images 
        :param input_image is a np.ndarray with 3 channels
        :param segmentation_mask is a np.ndarray with 3 channels
        :param alpha is a float value

    """
    blended = np.zeros(input_image.size, dtype=np.float32)
    blended = input_image * alpha + segmentation_mask * (1 - alpha)
    return blended


def convert_state_dict(state_dict):
    """Converts a state dict saved from a dataParallel module to normal 
       module state_dict inplace
       :param state_dict is the loaded DataParallel model_state
    
    """
    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        name = k[7:]  # remove `module.`
        new_state_dict[name] = v
    return new_state_dict


def get_logger(logdir):
    logger = logging.getLogger('ptsemseg')
    ts = str(datetime.datetime.now()).split('.')[0].replace(" ", "_")
    ts = ts.replace(":", "_").replace("-","_")
    file_path = os.path.join(logdir, 'run_{}.log'.format(ts))
    hdlr = logging.FileHandler(file_path)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    hdlr.setFormatter(formatter)
    logger.addHandler(hdlr) 
    logger.setLevel(logging.INFO)
    return logger

def flatten(lst):
    tmp = [i.contiguous().view(-1,1) for i in lst]
    return torch.cat(tmp).view(-1)

def unflatten_like(vector, likeTensorList):
    # Takes a flat torch.tensor and unflattens it to a list of torch.tensors
    #    shaped like likeTensorList
    outList = []
    i=0
    for tensor in likeTensorList:
        #n = module._parameters[name].numel()
        n = tensor.numel()
        outList.append(vector[:,i:i+n].view(tensor.shape))
        i+=n
    return outList
    
def LogSumExp(x,dim=0):
    m,_ = torch.max(x,dim=dim,keepdim=True)
    return m + torch.log((x - m).exp().sum(dim=dim,keepdim=True))

def adjust_learning_rate(optimizer, lr):
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


def save_checkpoint(dir, epoch, name='checkpoint', **kwargs):
    state = {
        'epoch': epoch,
    }
    state.update(kwargs)
    filepath = os.path.join(dir, '%s-%d.pt' % (name, epoch))
    torch.save(state, filepath)


def train_epoch(loader, model, criterion, optimizer, cuda=True, regression=False, verbose=False, subset=None):
    loss_sum = 0.0
    correct = 0.0
    verb_stage = 0

    num_objects_current = 0
    num_batches = len(loader)

    model.train()

    if subset is not None:
        num_batches = int(num_batches * subset)
        loader = itertools.islice(loader, num_batches)

    if verbose:
        loader = tqdm.tqdm(loader, total=num_batches)

    for i, (input, target) in enumerate(loader):
        if cuda:
            input = input.cuda(non_blocking=True)
            target = target.cuda(non_blocking=True)

        loss, output = criterion(model, input, target)
        
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
            
        loss_sum += loss.data.item() * input.size(0)

        if not regression:
            pred = output.data.argmax(1, keepdim=True)
            correct += pred.eq(target.data.view_as(pred)).sum().item()

        num_objects_current += input.size(0)

        if verbose and 10 * (i + 1) / num_batches >= verb_stage + 1:
            print('Stage %d/10. Loss: %12.4f. Acc: %6.2f' % (
                verb_stage + 1, loss_sum / num_objects_current,
                correct / num_objects_current * 100.0
            ))
            verb_stage += 1
    
    return {
        'loss': loss_sum / num_objects_current,
        'accuracy': None if regression else correct / num_objects_current * 100.0
    }


def eval(loader, model, criterion, cuda=True, regression=False, verbose=False):
    loss_sum = 0.0
    correct = 0.0
    num_objects_total = len(loader.dataset)

    model.eval()

    with torch.no_grad():
        if verbose:
            loader = tqdm.tqdm(loader)
        for i, (input, target) in enumerate(loader):
            if cuda:
                input = input.cuda(non_blocking=True)
                target = target.cuda(non_blocking=True)

            loss, output = criterion(model, input, target)

            loss_sum += loss.item() * input.size(0)

            if not regression:
                pred = output.data.argmax(1, keepdim=True)
                correct += pred.eq(target.data.view_as(pred)).sum().item()

    return {
        'loss': loss_sum / num_objects_total,
        'accuracy': None if regression else correct / num_objects_total * 100.0,
    }


def predict(loader, model, verbose=False):
    predictions = list()
    targets = list()

    model.eval()

    if verbose:
        loader = tqdm.tqdm(loader)

    offset = 0
    with torch.no_grad():
        for input, target in loader:
            input = input.cuda(non_blocking=True)
            output = model(input)

            batch_size = input.size(0)
            predictions.append(F.softmax(output, dim=1).cpu().numpy())
            targets.append(target.numpy())
            offset += batch_size

    return {
        'predictions': np.vstack(predictions),
        'targets': np.concatenate(targets)
    }


def moving_average(net1, net2, alpha=1):
    for param1, param2 in zip(net1.parameters(), net2.parameters()):
        param1.data *= (1.0 - alpha)
        param1.data += param2.data * alpha


def _check_bn(module, flag):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        flag[0] = True


def check_bn(model):
    flag = [False]
    model.apply(lambda module: _check_bn(module, flag))
    return flag[0]


def reset_bn(module):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        module.running_mean = torch.zeros_like(module.running_mean)
        module.running_var = torch.ones_like(module.running_var)


def _get_momenta(module, momenta):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        momenta[module] = module.momentum


def _set_momenta(module, momenta):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        module.momentum = momenta[module]


def bn_update(loader, model, verbose=False, subset=None, **kwargs):
    """
        BatchNorm buffers update (if any).
        Performs 1 epochs to estimate buffers average using train dataset.

        :param loader: train dataset loader for buffers average estimation.
        :param model: model being update
        :return: None
    """
    if not check_bn(model):
        return
    model.train()
    momenta = {}
    model.apply(reset_bn)
    model.apply(lambda module: _get_momenta(module, momenta))
    n = 0
    num_batches = len(loader)

    with torch.no_grad():
        if subset is not None:
            num_batches = int(num_batches * subset)
            loader = itertools.islice(loader, num_batches)
        if verbose:

            loader = tqdm.tqdm(loader, total=num_batches)
        for input, _ in loader:
            input = input.cuda(non_blocking=True)
            input_var = torch.autograd.Variable(input)
            b = input_var.data.size(0)

            momentum = b / (n + b)
            for module in momenta.keys():
                module.momentum = momentum

            model(input_var, **kwargs)
            n += b

    model.apply(lambda module: _set_momenta(module, momenta))

def inv_softmax(x, eps = 1e-10):
    return torch.log(x/(1.0 - x + eps))

def predictions(test_loader, model, seed=None, cuda=True, regression=False, **kwargs):
    #will assume that model is already in eval mode
    #model.eval()
    preds = []
    targets = []
    for input, target in test_loader:
        if seed is not None:
            torch.manual_seed(seed)
        if cuda:
            input = input.cuda(non_blocking=True)
        output = model(input, **kwargs)
        if regression:
            preds.append(output.cpu().data.numpy())
        else:
            probs = F.softmax(output, dim=1)
            preds.append(probs.cpu().data.numpy())
        targets.append(target.numpy())
    return np.vstack(preds), np.concatenate(targets)

def schedule(epoch, lr_init, epochs, swa, swa_start=None, swa_lr=None):
    t = (epoch) / (swa_start if swa else epochs)
    lr_ratio = swa_lr / lr_init if swa else 0.01
    if t <= 0.5:
        factor = 1.0
    elif t <= 0.9:
        factor = 1.0 - (1.0 - lr_ratio) * (t - 0.5) / 0.4
    else:
        factor = lr_ratio
    return lr_init * factor


def parseEightCameras(images, labels, aux, device):
    # Stack 8 Cameras into 1 for MCDO Dataset Testing
    images = torch.cat(images, 0)
    labels = torch.cat(labels, 0)
    aux = torch.cat(aux, 0)

    images = images.to(device)
    labels = labels.to(device)

    if len(aux.shape) < len(images.shape):
        aux = aux.unsqueeze(1).to(device)
        depth = torch.cat((aux, aux, aux), 1)
    else:
        aux = aux.to(device)
        depth = torch.cat((aux[:, 0, :, :].unsqueeze(1),
                           aux[:, 1, :, :].unsqueeze(1),
                           aux[:, 2, :, :].unsqueeze(1)), 1)

    fused = torch.cat((images, aux), 1)

    rgb = torch.cat((images[:, 0, :, :].unsqueeze(1),
                     images[:, 1, :, :].unsqueeze(1),
                     images[:, 2, :, :].unsqueeze(1)), 1)

    inputs = {"rgb": rgb,
              "d": depth,
              "rgbd": fused,
              "fused": fused}

    return inputs, labels


def plotPrediction(logdir, cfg, n_classes, i, i_val, k, inputs, pred, gt):
    fig, axes = plt.subplots(3, 4)
    [axi.set_axis_off() for axi in axes.ravel()]

    gt_norm = gt[0, :, :].copy()
    pred_norm = pred[0, :, :].copy()

    # Ensure each mask has same min and max value for matplotlib normalization
    gt_norm[0, 0] = 0
    gt_norm[0, 1] = n_classes
    pred_norm[0, 0] = 0
    pred_norm[0, 1] = n_classes

    axes[0, 0].imshow(inputs['rgb'][0, :, :, :].permute(1, 2, 0).cpu().numpy()[:, :, 0])
    axes[0, 0].set_title("RGB")

    axes[0, 1].imshow(inputs['d'][0, :, :, :].permute(1, 2, 0).cpu().numpy())
    axes[0, 1].set_title("D")

    axes[0, 2].imshow(gt_norm)
    axes[0, 2].set_title("GT")

    axes[0, 3].imshow(pred_norm)
    axes[0, 3].set_title("Pred")

    # axes[2,0].imshow(conf[0,:,:])
    # axes[2,0].set_title("Conf")

    # if len(cfg['models'])>1:
    #     if cfg['models']['rgb']['learned_uncertainty'] == 'yes':            
    #         channels = int(mean_outputs['rgb'].shape[1]/2)

    #         axes[1,1].imshow(mean_outputs['rgb'][:,channels:,:,:].mean(1)[0,:,:].cpu().numpy())
    #         axes[1,1].set_title("Aleatoric (RGB)")

    #         axes[1,2].imshow(mean_outputs['d'][:,channels:,:,:].mean(1)[0,:,:].cpu().numpy())
    #         # axes[1,2].imshow(mean_outputs['rgb'][:,:channels,:,:].mean(1)[0,:,:].cpu().numpy())
    #         axes[1,2].set_title("Aleatoric (D)")

    #     else:
    #         channels = int(mean_outputs['rgb'].shape[1])

    #     if cfg['models']['rgb']['mcdo_passes']>1:
    #         axes[2,1].imshow(var_outputs['rgb'][:,:channels,:,:].mean(1)[0,:,:].cpu().numpy())
    #         axes[2,1].set_title("Epistemic (RGB)")

    #         axes[2,2].imshow(var_outputs['d'][:,:channels,:,:].mean(1)[0,:,:].cpu().numpy())
    #         # axes[2,2].imshow(var_outputs['rgb'][:,channels:,:,:].mean(1)[0,:,:].cpu().numpy())
    #         axes[2,2].set_title("Epistemic (D)")

    path = "{}/{}".format(logdir, k)
    if not os.path.exists(path):
        os.makedirs(path)
    plt.tight_layout()
    plt.savefig("{}/{}_{}.png".format(path, i_val, i))
    plt.close(fig)


def plotMeansVariances(logdir, cfg, n_classes, i, i_val, m, k, inputs, pred, gt, mean, variance):
    fig, axes = plt.subplots(4, n_classes // 2 + 1)
    [axi.set_axis_off() for axi in axes.ravel()]

    for c in range(n_classes):
        mean_c = mean[0, c, :, :].cpu().numpy()
        variance_c = variance[0, c, :, :].cpu().numpy()

        axes[2 * (c % 2), c // 2].imshow(mean_c)
        axes[2 * (c % 2), c // 2].set_title(str(c) + " Mean")

        axes[2 * (c % 2) + 1, c // 2].imshow(variance_c)
        axes[2 * (c % 2) + 1, c // 2].set_title(str(c) + " Var")

    axes[-1, -1].imshow(variance[0, :, :, :].mean(0).cpu().numpy())
    axes[-1, -1].set_title("Average Variance")

    path = "{}/{}/{}/{}".format(logdir, "meanvar", m, k)
    if not os.path.exists(path):
        os.makedirs(path)
    plt.savefig("{}/{}_{}.png".format(path, i_val, i))
    plt.close(fig)

    fig, axes = plt.subplots(1, 2)
    axes[0].imshow(variance[0, :, :, :].mean(0).cpu().numpy())
    axes[1].imshow(mean[0, :, :, :].max(0)[0].cpu().numpy())
    plt.savefig("{}/{}_{}avg.png".format(path, i_val, i))
    plt.close(fig)