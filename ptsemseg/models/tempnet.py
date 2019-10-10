import torch.nn as nn
from torch.autograd import Variable

from .fusion.fusion import *
from ptsemseg.models.segnet_mcdo import *
from ptsemseg.utils import mutualinfo_entropy, plotEverything, plotPrediction


class TempNet(nn.Module):
    def __init__(self,
                 modality = 'rgb',
                 n_classes=21,
                 in_channels=3,
                 mcdo_passes=1,
                 full_mcdo=False,
                 freeze_seg=True,
                 freeze_temp=True,
                 dropoutP = 0,
                 scaling_module='None',
                 pretrained_rgb=None,
                 pretrained_d=None
                 ):
        super(TempNet, self).__init__()


        self.modality = modality

        self.segnet = segnet_mcdo(modality = self.modality,
                                  n_classes=n_classes,
                                  mcdo_passes=mcdo_passes,
                                  dropoutP=dropoutP,
                                  full_mcdo=full_mcdo,
                                  in_channels=in_channels,
                                  temperatureScaling=False,
                                  freeze_seg=freeze_seg,
                                  freeze_temp=freeze_temp, )

        # initialize temp net
        
        self.temp_down1 =  segnetDown2(in_channels*2, 64)
        self.temp_down2 = segnetDown2(64, 128)
        self.temp_up2 = segnetUp2(128, 64)
        self.temp_up1 = segnetUp2(64, 1)

        self.segnet = torch.nn.DataParallel(self.segnet, device_ids=range(torch.cuda.device_count()))

        if self.modality == 'rgb':
            #self.modality = "rgb"
            self.loadModel(self.segnet, pretrained_rgb)

        elif self.modality == 'd':
            #self.modality = "d"
            self.loadModel(self.segnet, pretrained_d)

        #else:
        #    print("no pretrained given")

        # freeze segnet networks
        for param in self.segnet.parameters():
            param.requires_grad = False

        self.softmaxMCDO = torch.nn.Softmax(dim=1)
        self.scale_logits = self._get_scale_module(scaling_module)

    def forward(self,inputs,inputs2,scaling_metrics="softmax entropy"):

        # Freeze batchnorm
        self.segnet.eval()

        # computer logits and uncertainty measures
        up1 = self.segnet.module.forwardMCDO_logits(inputs) #(batch,11,512,512,passes)

        inputs2 = torch.cat((inputs,inputs2),1)
        #import ipdb;ipdb.set_trace()
        tdown1, tindices_1, tunpool_shape1 = self.temp_down1(inputs2)
        tdown2, tindices_2, tunpool_shape2 = self.temp_down2(tdown1)
        tup2 = self.temp_up2(tdown2, tindices_2, tunpool_shape2)
        tup1 = self.temp_up1(tup2, tindices_1, tunpool_shape1) #[batch,1,512,512]
        temp = tup1.mean((2,3)).unsqueeze(-1).unsqueeze(-1) #(batch,1,1,1)
        tup1 = tup1.masked_fill(tup1 < 0.3, 0.3)
        #tup1 = tup1.masked_fill(tup1 > 1, 1)
        x = up1 #* tup1.unsqueeze(-1)
        mean = x.mean(-1) #[batch,classes,512,512]
        variance = x.std(-1)
    
        prob = self.softmaxMCDO(x) #[batch,classes,512,512]
        prob = prob.masked_fill(prob < 1e-9, 1e-9)
        entropy,mutual_info = mutualinfo_entropy(prob)#(batch,512,512)
        if self.scale_logits != None:
          DR = self.scale_logits(variance, entropy,mutual_info, tup1.squeeze(1),mode=scaling_metrics) #(batch,1,1,1)
          #mean = mean * torch.min(tup1,DR)
          mean = mean * DR
        return mean, variance, entropy, mutual_info,tup1.squeeze(1), temp.view(-1),entropy.mean((1,2)),mutual_info.mean((1,2))

    def loadModel(self, model, path):
        model_pkl = path

        print(path)
        if os.path.isfile(model_pkl):
            pretrained_dict = torch.load(model_pkl)['model_state']
            model_dict = model.state_dict()
            #import ipdb;ipdb.set_trace()
            # 1. filter out unnecessary keys
            pretrained_dict = {k: v.resize_(model_dict[k].shape) for k, v in pretrained_dict.items() if (
                    k in model_dict)}  # and ((model!="fuse") or (model=="fuse" and not start_layer in k))}
            print("Loaded {} pretrained parameters".format(len(pretrained_dict)))
            # 2. overwrite entries in the existing state dict
            model_dict.update(pretrained_dict)
            #import ipdb;ipdb.set_trace()
            # 3. load the new state dict
            model.load_state_dict(pretrained_dict)
            #import ipdb;ipdb.set_trace()
        else:
            print("no pretrained given")
            #exit()

    def _get_scale_module(self, name, n_classes=11, bias_init=None):

        name = str(name)

        return {
            "temperature": TemperatureScaling(n_classes, bias_init),
            "uncertainty": UncertaintyScaling(n_classes, bias_init),
            "LocalUncertaintyScaling": LocalUncertaintyScaling(n_classes, bias_init),
            "GlobalUncertainty": GlobalUncertaintyScaling(n_classes, bias_init),
            "GlobalLocalUncertainty": GlobalLocalUncertaintyScaling(n_classes, bias_init),
            "GlobalScaling" : GlobalScaling(modality=self.modality),
            "None": None
        }[name]
