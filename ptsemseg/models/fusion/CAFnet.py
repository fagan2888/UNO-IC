import torch.nn as nn
from torch.autograd import Variable

from .fusion import *
from ptsemseg.models.recalibrator import *
from ptsemseg.models.segnet_mcdo import *


class CAFnet(nn.Module):
    def __init__(self,
                 backbone="segnet",
                 n_classes=21,
                 in_channels=3,
                 is_unpooling=True,
                 input_size=(473, 473),
                 batch_size=2,
                 version=None,
                 mcdo_passes=1,
                 dropoutP=0.1,
                 full_mcdo=False,
                 start_layer="down1",
                 end_layer="up1",
                 reduction=1.0,
                 device="cpu",
                 recalibration="None",
                 recalibrator="None",
                 bins=0,
                 temperatureScaling=False,
                 freeze_seg=True,
                 freeze_temp=True,
                 fusion_module="1.3",
                 pretrained_rgb="./models/Segnet/rgb_Segnet/rgb_segnet_mcdo_airsim_T000+T050.pkl",
                 pretrained_d="./models/Segnet/d_Segnet/d_segnet_mcdo_airsim_T000+T050.pkl"
                 ):
        super(CAFnet, self).__init__()

        self.rgb_segnet = segnet_mcdo(n_classes=n_classes,
                                      input_size=input_size,
                                      batch_size=batch_size,
                                      version=version,
                                      reduction=reduction,
                                      mcdo_passes=mcdo_passes,
                                      dropoutP=dropoutP,
                                      full_mcdo=full_mcdo,
                                      in_channels=in_channels,
                                      start_layer=start_layer,
                                      end_layer=end_layer,
                                      device=device,
                                      recalibration=recalibration,
                                      recalibrator=recalibrator,
                                      temperatureScaling=temperatureScaling,
                                      freeze_seg=freeze_seg,
                                      freeze_temp=freeze_temp,
                                      bins=bins)

        self.d_segnet = segnet_mcdo(n_classes=n_classes,
                                    input_size=input_size,
                                    batch_size=batch_size,
                                    version=version,
                                    reduction=reduction,
                                    mcdo_passes=mcdo_passes,
                                    dropoutP=dropoutP,
                                    full_mcdo=full_mcdo,
                                    in_channels=in_channels,
                                    start_layer=start_layer,
                                    end_layer=end_layer,
                                    device=device,
                                    recalibration=recalibration,
                                    recalibrator=recalibrator,
                                    temperatureScaling=temperatureScaling,
                                    freeze_seg=freeze_seg,
                                    freeze_temp=freeze_temp,
                                    bins=bins)

        self.rgb_segnet = torch.nn.DataParallel(self.rgb_segnet, device_ids=range(torch.cuda.device_count()))
        self.d_segnet = torch.nn.DataParallel(self.d_segnet, device_ids=range(torch.cuda.device_count()))

        # initialize segnet weights
        if pretrained_rgb:
            self.loadModel(self.rgb_segnet, pretrained_rgb)
        if pretrained_d:
            self.loadModel(self.d_segnet, pretrained_d)

        # freeze segnet networks
        for param in self.rgb_segnet.parameters():
            param.requires_grad = False
        for param in self.d_segnet.parameters():
            param.requires_grad = False

        self.fusion = self._get_fusion_module(fusion_module)(n_classes)



    def forward(self, inputs):

        # Freeze batchnorm
        self.rgb_segnet.eval()
        self.d_segnet.eval()

        inputs_rgb = inputs[:, :3, :, :]
        inputs_d = inputs[:, 3:, :, :]

        mean = {}
        variance = {}
        entropy = {}
        MI = {}

        mean['rgb'], variance['rgb'], entropy['rgb'], MI['rgb'] = self.rgb_segnet.module.forwardMCDO(inputs_rgb)
        mean['d'], variance['d'], entropy['d'], MI['d'] = self.d_segnet.module.forwardMCDO(inputs_d)

        s = variance['rgb'].shape
        variance['rgb'] = torch.mean(variance['rgb'], 1).view(-1, 1, s[2], s[3])
        variance['d'] = torch.mean(variance['d'], 1).view(-1, 1, s[2], s[3])

        x = self.fusion(mean, variance)
        #for param in self.fusion.parameters():
        #    print(param.data)

        #import ipdb; ipdb.set_trace()
        #print(x)

        return x

    def loadModel(self, model, path):
        model_pkl = path

        print(path)
        if os.path.isfile(model_pkl):
            pretrained_dict = torch.load(model_pkl)['model_state']
            model_dict = model.state_dict()

            # 1. filter out unnecessary keys
            pretrained_dict = {k: v.resize_(model_dict[k].shape) for k, v in pretrained_dict.items() if (
                    k in model_dict)}  # and ((model!="fuse") or (model=="fuse" and not start_layer in k))}

            # 2. overwrite entries in the existing state dict
            model_dict.update(pretrained_dict)

            # 3. load the new state dict
            model.load_state_dict(pretrained_dict)
        else:
            print("model not found")
            exit()

    def _get_fusion_module(self, name):

        name = str(name)

        return {
            "GatedFusion": GatedFusion,
            "1.0": GatedFusion,
            "ConditionalAttentionFusion": ConditionalAttentionFusion,
            "1.1": ConditionalAttentionFusion,

            "PreweightedGatedFusion": PreweightedGatedFusion,
            "1.2": PreweightedGatedFusion,
            "UncertaintyGatedFusion": UncertaintyGatedFusion,
            "1.3": UncertaintyGatedFusion,
            "ConditionalAttentionFusionv2": ConditionalAttentionFusionv2,
            "2.1": ConditionalAttentionFusionv2,
            "ScaledAverage": ScaledAverage,
        }[name]
