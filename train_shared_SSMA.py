import matplotlib

matplotlib.use('Agg')

import os
import yaml
import time
import shutil
import torch
import random
import argparse

from tqdm import tqdm
import matplotlib.pyplot as plt

from ptsemseg.models import get_model
from ptsemseg.loss import get_loss_function
from ptsemseg.loader import get_loaders
from ptsemseg.utils import get_logger, parseEightCameras, plotPrediction, plotMeansVariances, plotEntropy, plotMutualInfo, mutualinfo_entropy, plotEverything
from ptsemseg.metrics import runningScore, averageMeter
from ptsemseg.schedulers import get_scheduler
from ptsemseg.optimizers import get_optimizer
from ptsemseg.degredations import *
from tensorboardX import SummaryWriter

from functools import partial
from collections import defaultdict
import time
# SWAG lib imports
from ptsemseg.posteriors import SWAG
from ptsemseg.utils import bn_update, mem_report

global logdir, cfg, n_classes, i, i_val, k

def plot_grad_flow(module, i=0):
    ave_grads = []
    layers = []
    for n, p in module.named_parameters():
        if (p.requires_grad) and ("bias" not in n):

            for _ in range(len(p)):
                layers.append(_)
                ave_grads.append(p.grad[_].abs().mean().item())
    print(layers, ave_grads)
    plt.plot(ave_grads, alpha=0.3, color="b")
    plt.xlabel("Layers")
    plt.ylabel("average gradient")
    plt.title("Gradient flow")
    plt.grid(True)

    if (i + 1) % 50 == 0:
        plt.savefig("gradient_flow_{}".format(i))


def random_seed(seed_value, use_cuda):
    np.random.seed(seed_value)  # cpu vars
    torch.manual_seed(seed_value)  # cpu  vars
    random.seed(seed_value)  # Python
    if use_cuda:
        torch.cuda.manual_seed(seed_value)
        torch.cuda.manual_seed_all(seed_value)  # gpu vars
        torch.backends.cudnn.deterministic = True  # needed
        torch.backends.cudnn.benchmark = False


def train(cfg, writer, logger, logdir):
    # log git commit
    import subprocess
    label = subprocess.check_output(["git", "describe", "--always"]).strip()
    logger.info("Using commit {}".format(label))

    # Setup seeds
    random_seed(1337, True)

    # Setup device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Setup Dataloader
    loaders, n_classes = get_loaders(cfg["data"]["dataset"], cfg)

    # Setup Metrics
    running_metrics_val = {env: runningScore(n_classes) for env in loaders['val'].keys()}

    # Setup Meters
    val_loss_meter = {m: {env: averageMeter() for env in loaders['val'].keys()} for m in cfg["models"].keys()}
    val_CE_loss_meter = {env: averageMeter() for env in loaders['val'].keys()}
    val_REG_loss_meter = {env: averageMeter() for env in loaders['val'].keys()}
    variance_meter = {m: {env: averageMeter() for env in loaders['val'].keys()} for m in cfg["models"].keys()}
    entropy_meter = {m: {env: averageMeter() for env in loaders['val'].keys()} for m in cfg["models"].keys()}
    mutual_info_meter = {m: {env: averageMeter() for env in loaders['val'].keys()} for m in cfg["models"].keys()}
    time_meter = averageMeter()

    # set seeds for training
    random_seed(cfg['seed'], True)

    start_iter = 0
    models = {}
    swag_models = {}
    optimizers = {}
    schedulers = {}
    best_iou = -100.0

    # Setup Model
    for model, attr in cfg['models'].items():

        attr = defaultdict(lambda: None, attr)

        models[model] = get_model(name=attr['arch'],
                                  modality=model,
                                  n_classes=n_classes,
                                  input_size=(cfg['data']['img_rows'], cfg['data']['img_cols']),
                                  in_channels=attr['in_channels'],
                                  mcdo_passes=attr['mcdo_passes'],
                                  dropoutP=attr['dropoutP'],
                                  full_mcdo=attr['full_mcdo'],
                                  device=device,
                                  temperatureScaling=cfg['temperatureScaling'],
                                  freeze_seg=cfg['freeze_seg'],
                                  freeze_temp=cfg['freeze_temp'],
                                  pretrained_rgb=cfg['pretrained_rgb'],
                                  pretrained_d=cfg['pretrained_d'],
                                  fusion_module=cfg['fusion_module'],
                                  scaling_module=cfg['scaling_module']).to(device)

        models[model] = torch.nn.DataParallel(models[model], device_ids=range(torch.cuda.device_count()))

        # Setup optimizer, lr_scheduler and loss function
        optimizer_cls = get_optimizer(cfg)
        optimizer_params = {k: v for k, v in cfg['training']['optimizer'].items()
                            if k != 'name'}

        optimizers[model] = optimizer_cls(models[model].parameters(), **optimizer_params)
        logger.info("Using optimizer {}".format(optimizers[model]))

        schedulers[model] = get_scheduler(optimizers[model], cfg['training']['lr_schedule'])

        loss_fn = get_loss_function(cfg)
        # loss_fn_A = get_loss_function(cfg)
        # loss_fn_B = get_loss_function(cfg)
        logger.info("Using loss {}".format(loss_fn))

        # setup swa training
        if cfg['swa']:
            print('SWAG training')
            swag_models[model] = SWAG(models[model],
                                      no_cov_mat=False,
                                      max_num_models=20)

            swag_models[model].to(device)
        else:
            print('SGD training')

        # Load pretrained weights
        if str(attr['resume']) != "None":

            model_pkl = attr['resume']

            if os.path.isfile(model_pkl):
                logger.info(
                    "Loading model and optimizer from checkpoint '{}'".format(model_pkl)
                )

                checkpoint = torch.load(model_pkl)

                pretrained_dict = checkpoint['model_state']
                model_dict = models[model].state_dict()

                # 1. filter out unnecessary keys
                pretrained_dict = {k: v.resize_(model_dict[k].shape) for k, v in pretrained_dict.items() if (
                        k in model_dict)}  # and ((model!="fuse") or (model=="fuse" and not start_layer in k))}

                # 2. overwrite entries in the existing state dict
                model_dict.update(pretrained_dict)

                # 3. load the new state dict
                models[model].load_state_dict(pretrained_dict, strict=False)

                if attr['resume'] == 'same_yaml':
                    optimizers[model].load_state_dict(checkpoint["optimizer_state"])
                    schedulers[model].load_state_dict(checkpoint["scheduler_state"])

                # resume iterations only if specified
                if cfg['training']['resume_iteration'] or str(cfg['training']['resume_iteration']) == "None":
                    start_iter = checkpoint["epoch"]

                # start_iter = 0
                logger.info("Loaded checkpoint '{}' (iter {}), parameters {}".format(model_pkl, checkpoint["epoch"],len(pretrained_dict.keys())))
                print("Loaded checkpoint '{}' (iter {}), parameters {}".format(model_pkl, checkpoint["epoch"],len(pretrained_dict.keys())))
            else:
                logger.info("No checkpoint found at '{}'".format(model_pkl))
                print("No checkpoint found at '{}'".format(model_pkl))
                exit()

        if cfg['swa'] and str(cfg['swa']['resume']) != "None":
            if os.path.isfile(cfg['swa']['resume']):
                checkpoint = torch.load(cfg['swa']['resume'])
                swag_models[model].load_state_dict(checkpoint['model_state'])
            else:
                logger.info("No checkpoint found at '{}'".format(model_pkl))
                print("No checkpoint found at '{}'".format(model_pkl))
                exit()
                
        # setup weight for unbalanced dataset
        if cfg['training']['weight'] != "None":
            weight = torch.Tensor(cfg['training']['weight']).cuda()
        else:
            weight = None #[1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]

    plt.clf()
    i = start_iter
    print("Beginning Training at iteration: {}".format(i))
    while i < cfg["training"]["train_iters"]:

        #################################################################################
        # Training
        #################################################################################
        print("=" * 10, "TRAINING", "=" * 10)
        for (input_list, labels_list) in loaders['train']:

            i += 1

            inputs, labels = parseEightCameras(input_list['rgb'], labels_list, input_list['d'], device)

            # Read batch from only one camera
            bs = cfg['training']['batch_size']
            images = {m: inputs[m][:bs, :, :, :] for m in cfg["models"].keys()}
            labels = labels[:bs, :, :]

            if labels.shape[0] <= 1:
                continue

            start_ts = time.time()

            [schedulers[m].step() for m in schedulers.keys()]
            [models[m].train() for m in models.keys()]
            [optimizers[m].zero_grad() for m in optimizers.keys()]

            # Run Models
            outputs = {}
            outputs_A = {}
            outputs_B = {}
            loss = {}
            loss_A = {}
            loss_B = {}
            for m in cfg["models"].keys():
                if cfg["models"][m]["arch"] == "tempnet":
                    if m == 'rgb':
                        m_temp = 'd'
                    else:
                        m_temp = 'rgb' 
                    outputs[m], _, _,_,_,_,_,_ = models[m](images[m],images[m_temp])
                else:
                    outputs_A[m],outputs_B[m],outputs[m],_,_,_,_,_ = models[m](images[m])

                loss[m] = loss_fn(input=outputs[m], target=labels, weight=weight)

                loss_A[m] = loss_fn(input=outputs_A[m], target=labels, weight=weight)

                loss_B[m] = loss_fn(input=outputs_B[m], target=labels, weight=weight)
                sum([loss[m],loss_A[m],loss_B[m]]).backward()

                # for n, p in models[m].module.named_parameters():
                    # if (p.requires_grad) and ("bias" not in n):
                        # print(n, p, p.grad)

                # plot_grad_flow(models[m].module.fusion, i)

                optimizers[m].step()
            time_meter.update(time.time() - start_ts)
            if (i + 1) % cfg['training']['print_interval'] == 0:
                for m in cfg["models"].keys():
                    fmt_str = "Iter [{:d}/{:d}]  Loss {}: {:.4f}  Time/Image: {:.4f}"
                    print_str = fmt_str.format(i + 1,
                                               cfg['training']['train_iters'],
                                               m,
                                               loss[m].item(),
                                               time_meter.avg / cfg['training']['batch_size'])

                    print(print_str)
                    logger.info(print_str)
                    writer.add_scalar('loss/train_loss/' + m, loss[m].item(), i + 1)
                time_meter.reset()

            # collect parameters for swa
            if cfg['swa'] and (i + 1 - cfg['swa']['start']) % cfg['swa']['c_iterations'] == 0:
                print('Saving SWA model at iteration: ', i + 1)
                swag_models[m].collect_model(models[m])

            if i % cfg["training"]["val_interval"] == 0 or i >= cfg["training"]["train_iters"]:

                [models[m].eval() for m in models.keys()]

                if cfg['swa']:
                    print('Updating SWA model')
                    swag_models[m].sample(0.0)
                    bn_update(loaders['train'], swag_models[m], m)
                #################################################################################
                # Validation
                #################################################################################
                print("=" * 10, "VALIDATING", "=" * 10)

                with torch.no_grad():
                    for k, valloader in loaders['val'].items():
                        for i_val, (input_list, labels_list) in tqdm(enumerate(valloader)):

                            inputs, labels = parseEightCameras(input_list['rgb'], labels_list, input_list['d'], device)
                            inputs_display, _ = parseEightCameras(input_list['rgb_display'], labels_list,
                                                                  input_list['d_display'], device)

                            # Read batch from only one camera
                            bs = cfg['training']['batch_size']

                            images_val = {m: inputs[m][:bs, :, :, :] for m in cfg["models"].keys()}
                            labels_val = labels[:bs, :, :]

                            if labels_val.shape[0] <= 1:
                                continue

                            # Run Models
                            mean = {}
                            mean_A = {}
                            mean_B = {}
                            variance = {}
                            entropy = {}
                            mutual_info = {}
                            temp_map = {}
                            val_loss = {}

                            for m in cfg["models"].keys():

                                entropy[m] = torch.zeros(labels_val.shape)
                                mutual_info[m] = torch.zeros(labels_val.shape)

                                if cfg['swa']:
                                    mean[m] = swag_models[m](images_val[m])
                                    variance[m] = torch.zeros(mean[m].shape)
                                elif hasattr(models[m].module, 'forwardMCDO'):
                                    mean[m], variance[m], entropy[m], mutual_info[m] = models[m].module.forwardMCDO(
                                        images_val[m],mcdo=False)
                                elif cfg["models"][m]["arch"] == "tempnet":
                                        if m == 'rgb':
                                            m_temp = 'd'
                                        else:
                                            m_temp = 'rgb' 
                                        mean[m], variance[m], entropy[m], mutual_info[m], temp_map[m],_,_,_ = models[m](images_val[m],images_val[m_temp])
                                else:
                                    # _,_,mean[m],_,_,_,_,_ = models[m](images_val[m])
                                    mean_A[m],mean_B[m],mean[m],_,_,_,_,_ = models[m](images_val[m])
                                    variance[m] = torch.zeros(mean[m].shape)

                                val_loss[m] = loss_fn(input=mean[m], target=labels_val, weight=weight)

                            # Fusion Type
                            if cfg["fusion"] == "None":
                                outputs = torch.nn.Softmax(dim=1)(mean[list(cfg["models"].keys())[0]])
                                outputs_A = torch.nn.Softmax(dim=1)(mean_A[list(cfg["models"].keys())[0]])
                                outputs_B = torch.nn.Softmax(dim=1)(mean_B[list(cfg["models"].keys())[0]])
                            elif cfg["fusion"] == "SoftmaxMultiply":
                                outputs = torch.nn.Softmax(dim=1)(mean["rgb"]) * torch.nn.Softmax(dim=1)(mean["d"])
                            elif cfg["fusion"] == "SoftmaxAverage":
                                outputs = torch.nn.Softmax(dim=1)(mean["rgb"]) + torch.nn.Softmax(dim=1)(mean["d"])
                            elif cfg["fusion"] == "WeightedVariance":
                                rgb_var = 1 / (torch.mean(variance["rgb"], 1) + 1e-5)
                                d_var = 1 / (torch.mean(variance["d"], 1) + 1e-5)

                                rgb = torch.nn.Softmax(dim=1)(mean["rgb"])
                                d = torch.nn.Softmax(dim=1)(mean["d"])
                                for n in range(n_classes):
                                    rgb[:, n, :, :] = rgb[:, n, :, :] * rgb_var
                                    d[:, n, :, :] = d[:, n, :, :] * d_var
                                outputs = rgb + d
                            elif cfg["fusion"] == "Noisy-Or":
                                outputs = 1 - (1 - torch.nn.Softmax(dim=1)(mean["rgb"])) * (
                                        1 - torch.nn.Softmax(dim=1)(mean["d"]))
                            else:
                                print("Fusion Type Not Supported")

                            # plot ground truth vs mean/variance of outputs
                            outputs = outputs/outputs.sum(1).unsqueeze(1)
                            prob, pred = outputs.max(1)
                            gt = labels_val
                            outputs_A = outputs_A/outputs_A.sum(1).unsqueeze(1)
                            outputs_B = outputs_B/outputs_B.sum(1).unsqueeze(1)
                            _,pred_A = outputs_A.max(1)
                            _,pred_B = outputs_B.max(1)
                            # e, _ = mutualinfo_entropy(outputs.unsqueeze(-1))

                            if i_val % cfg["training"]["png_frames"] == 0:
                                plotPrediction(logdir, cfg, n_classes, i, i_val, k, inputs_display, pred, gt)
                                plotPrediction(logdir, cfg, n_classes, i, i_val, k + '/rgb', inputs_display, pred_A, gt)
                                plotPrediction(logdir, cfg, n_classes, i, i_val, k + '/depth', inputs_display, pred_B, gt)
                                # labels = ['entropy', 'probability']
                                # values = [e, prob]
                                # plotEverything(logdir, i, i_val, k + "/fused", values, labels)

                                # for m in cfg["models"].keys():
                                #     prob,pred_m = torch.nn.Softmax(dim=1)(mean[m]).max(1)
                                #     if cfg["models"][m]["arch"] == "tempnet":
                                #         labels = ['mutual info', 'entropy', 'probability','temperature']
                                #         values = [mutual_info[m], entropy[m], prob, temp_map[m]]
                                #     else:
                                #         labels = ['mutual info', 'entropy', 'probability']
                                #         values = [mutual_info[m], entropy[m], prob]
                                #     plotPrediction(logdir, cfg, n_classes, i, i_val, k + "/" + m, inputs_display, pred_m, gt)
                                #     plotEverything(logdir, i, i_val, k + "/" + m, values, labels)

                            running_metrics_val[k].update(gt.cpu().numpy(), pred.cpu().numpy())

                            for m in cfg["models"].keys():
                                val_loss_meter[m][k].update(val_loss[m].item())
                                variance_meter[m][k].update(torch.mean(variance[m]).item())
                                entropy_meter[m][k].update(torch.mean(entropy[m]).item())
                                mutual_info_meter[m][k].update(torch.mean(mutual_info[m]).item())

                    for m in cfg["models"].keys():
                        for k in loaders['val'].keys():
                            writer.add_scalar('loss/val_loss/{}/{}'.format(m, k), val_loss_meter[m][k].avg, i + 1)
                            logger.info("%s %s Iter %d Loss: %.4f" % (m, k, i, val_loss_meter[m][k].avg))

                mean_iou = 0

                for env, valloader in loaders['val'].items():
                    logger.info(env)
                    score, class_iou = running_metrics_val[env].get_scores()
                    for k, v in score.items():
                        logger.info('{}: {}'.format(k, v))
                        writer.add_scalar('val_metrics/{}/{}'.format(env, k), v, i)

                    for k, v in class_iou.items():
                        logger.info('{}: {}'.format(k, v))
                        writer.add_scalar('val_metrics/{}/cls_{}'.format(env, k), v, i)

                    for m in cfg["models"].keys():
                        val_loss_meter[m][env].reset()
                        variance_meter[m][env].reset()
                        entropy_meter[m][env].reset()
                        mutual_info_meter[m][env].reset()
                    running_metrics_val[env].reset()

                    mean_iou += score["Mean IoU : \t"]

                mean_iou /= len(loaders['val'])

                # save models
                if i <= cfg["training"]["train_iters"]:

                    print("best iou so far: {}, current iou: {}".format(best_iou, mean_iou))

                    for m in optimizers.keys():
                        model = models[m]
                        optimizer = optimizers[m]
                        scheduler = schedulers[m]

                        if not os.path.exists(writer.file_writer.get_logdir() + "/best_model"):
                            os.makedirs(writer.file_writer.get_logdir() + "/best_model")

                        # save best model (averaging the best overall accuracies on the validation set)
                        if mean_iou > best_iou:
                            print('SAVING BEST MODEL')
                            best_iou = mean_iou
                            state = {
                                "epoch": i,
                                "model_state": model.state_dict(),
                                "optimizer_state": optimizer.state_dict(),
                                "scheduler_state": scheduler.state_dict(),
                                "mean_iou": mean_iou,
                            }
                            save_path = os.path.join(writer.file_writer.get_logdir(),
                                                     "best_model",
                                                     "{}_{}_{}_best_model.pkl".format(
                                                         m,
                                                         cfg['models'][m]['arch'],
                                                         cfg['data']['dataset']))
                            torch.save(state, save_path)

                            if cfg['swa'] and i > cfg['swa']['start']:
                                state = {
                                    "epoch": i,
                                    "model_state": swag_models[m].state_dict(),
                                    "mean_iou": mean_iou,
                                }
                                save_path = os.path.join(writer.file_writer.get_logdir(),
                                                         "best_model",
                                                         "{}_{}_{}_swag.pkl".format(
                                                             m,
                                                             cfg['models'][m]['arch'],
                                                             cfg['data']['dataset']))

                                torch.save(state, save_path)

                        # save models
                        if 'save_iters' not in cfg['training'].keys() or i % cfg['training']['save_iters'] == 0:
                            state = {
                                "epoch": i,
                                "model_state": model.state_dict(),
                                "optimizer_state": optimizer.state_dict(),
                                "scheduler_state": scheduler.state_dict(),
                                "mean_iou": mean_iou,
                            }
                            save_path = os.path.join(writer.file_writer.get_logdir(),
                                                     "{}_{}_{}_{}_model.pkl".format(
                                                         m,
                                                         cfg['models'][m]['arch'],
                                                         cfg['data']['dataset'],
                                                         i))
                            torch.save(state, save_path)

                        if cfg['swa'] and i > cfg['swa']['start']:
                            state = {
                                "epoch": i,
                                "model_state": swag_models[m].state_dict(),
                                "mean_iou": mean_iou,
                            }
                            save_path = os.path.join(writer.file_writer.get_logdir(),
                                                     "{}_{}_{}_swag.pkl".format(
                                                         m,
                                                         cfg['models'][m]['arch'],
                                                         cfg['data']['dataset']))

                            torch.save(state, save_path)

            if i >= cfg["training"]["train_iters"]:
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="config")
    parser.add_argument(
        "--config",
        nargs="?",
        type=str,
        default="configs/train/rgbd_BayesianSegnet_0.5_T000.yml",
        help="Configuration file to use",
    )

    parser.add_argument(
        "--tag",
        nargs="?",
        type=str,
        default="",
        help="Unique identifier for different runs",
    )

    parser.add_argument(
        "--run",
        nargs="?",
        type=str,
        default="",
        help="Directory to rerun",
    )

    parser.add_argument(
        "--seed",
        nargs="?",
        type=int,
        default=-1,
        help="Directory to rerun",
    )

    args = parser.parse_args()

    # cfg is a  with two-level dictionary ['training','data','model']['batch_size']
    if args.run != "":

        # find and load config
        for root, dirs, files in os.walk(args.run):
            for f in files:
                if '.yml' in f:
                    path = root + f
                    args.config = path

        with open(path) as fp:
            cfg = defaultdict(lambda: None, yaml.load(fp))

        # find and load saved best models
        for m in cfg['models'].keys():
            for root, dirs, files in os.walk(args.run):
                for f in files:
                    if m in f and '.pkl' in f:
                        cfg['models'][m]['resume'] = root + f

        logdir = args.run

    else:
        with open(args.config) as fp:
            cfg = defaultdict(lambda: None, yaml.load(fp))

        logdir = "/".join(["runs"] + args.config.split("/")[1:])[:-4]+'/'+cfg['id']

        # append tag 
        if args.tag:
            logdir += "/" + args.tag
        
        # set seed if flag set
        if args.seed != -1:
            cfg['seed'] = args.seed
            logdir += "/" + str(args.seed)
            
    # baseline train (concatenation, warping baselines)
    writer = SummaryWriter(logdir)
    path = shutil.copy(args.config, logdir)
    logger = get_logger(logdir)

    # generate seed if none present       
    if cfg['seed'] is None:
        seed = int(time.time())
        cfg['seed'] = seed

        # modify file to reflect seed
        with open(path, 'r') as original:
            data = original.read()
        with open(path, 'w') as modified:
            modified.write("seed: {}\n".format(seed) + data)
            
    print("using seed {}".format(cfg['seed']))

    train(cfg, writer, logger, logdir)

    print('done')
    time.sleep(10)
    writer.close()