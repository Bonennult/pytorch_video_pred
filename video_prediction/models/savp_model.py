# 作者：李溢
# 日期：2019/5/15

import functools
import itertools
import os
import re
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from collections import OrderedDict
from tensorflow.contrib.training import HParams
from video_prediction.utils import util
from video_prediction.utils.max_sv import spectral_normed_weight
from video_prediction.layers.conv import Conv2d, Conv3d
from video_prediction.layers.convLSTM import ConvLSTMCell
from video_prediction.models.modules import Prior, Posterior, Dense, Encoder


class SAVPCell(nn.Module):
    ### 假定 input_shape={'images':NCHW, 'zs':(N,nz)} 5/27
    ### 假定这里的 images 和 zs 都是经过 unroll_rnn 拆分过的 5/27
    def __init__(self, input_shape, mode, hparams):
        super(SAVPCell, self).__init__()
        self.mode = mode
        self.hparams = hparams
        self.input_shape = input_shape  ### 设定为一个dict 5/23
        self.batch_size = input_shape['images'][0]   ### images.shape=NCHW 5/23
        self.image_shape = list(input_shape['images'][-3:])
        color_channel, height, width = self.image_shape
        self.num_encoder_layers = 4
        #self.time = 0
        self.conv_rnn_state_sizes = []  ### 为初始化 state 做准备 6/1
        
        ############### latent code ###############
        ### LSTMCell inputs 应当是 (batch_size,input_size) 5/28
        self.lstm_z = nn.LSTMCell(input_size=self.input_shape['zs'][1], hidden_size=self.hparams.nz)
        self.rnn_z_state_sizes = [self.batch_size, self.hparams.nz]
        
        self.scale_size = min(height, width)
        if self.scale_size >= 256:
            raise NotImplementedError
        elif self.scale_size >=128:
            ############### encoder ###############
            ### savp_model #523 5/26
            conv_rnn_height, conv_rnn_width = height, width
            ### 第 0 层 5/26
            ### conv_pool + norm + activation 5/26
            conv_rnn_height, conv_rnn_width = conv_rnn_height//2, conv_rnn_width//2
            in_channel = self.input_shape['images'][1]*2 + self.hparams.nz
            out_channel = self.hparams.ngf
            ### 记录 conv rnn states 的 shape，便于初始化 6/1
            ### 注意初始化时要有两个相同shape的state，分别是 h 和 c 6/1
            self.conv_rnn_state_sizes.append([self.batch_size, out_channel, conv_rnn_height, conv_rnn_width])
            self.encoder_0_conv = nn.ModuleList()
            self.encoder_0_conv += [nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=5,
                                stride=1,
                                padding=(2,2))]
            self.encoder_0_conv += [nn.AvgPool2d(kernel_size=(2,2), stride=(2,2))]
            self.encoder_0_conv += [nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)]
            self.encoder_0_conv += [nn.ReLU()]
            ### 第 1 层 5/26
            conv_rnn_height, conv_rnn_width = conv_rnn_height//2, conv_rnn_width//2
            in_channel = out_channel + hparams.nz
            out_channel = self.hparams.ngf*2
            self.conv_rnn_state_sizes.append([self.batch_size, out_channel, conv_rnn_height, conv_rnn_width])
            self.encoder_1_conv = nn.ModuleList()
            self.encoder_1_conv += [nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))]
            self.encoder_1_conv += [nn.AvgPool2d(kernel_size=(2,2), stride=(2,2))]
            self.encoder_1_conv += [nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)]
            self.encoder_1_conv += [nn.ReLU()]
            self.encoder_1_rnn = ConvLSTMCell(
                                input_size=(conv_rnn_height,conv_rnn_width),
                                input_dim=out_channel + self.hparams.nz,
                                hidden_dim=out_channel)
            ### 第 2 层 5/26
            conv_rnn_height, conv_rnn_width = conv_rnn_height//2, conv_rnn_width//2
            in_channel = out_channel + hparams.nz
            out_channel = self.hparams.ngf*4
            self.conv_rnn_state_sizes.append([self.batch_size, out_channel, conv_rnn_height, conv_rnn_width])
            self.encoder_2_conv = nn.ModuleList()
            self.encoder_2_conv += [nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))]
            self.encoder_2_conv += [nn.AvgPool2d(kernel_size=(2,2), stride=(2,2))]
            self.encoder_2_conv += [nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)]
            self.encoder_2_conv += [nn.ReLU()]
            self.encoder_2_rnn = ConvLSTMCell(
                                input_size=(conv_rnn_height,conv_rnn_width),
                                input_dim=out_channel + self.hparams.nz,
                                hidden_dim=out_channel)
            ### 第 3 层 5/26
            conv_rnn_height, conv_rnn_width = conv_rnn_height//2, conv_rnn_width//2
            in_channel = out_channel + hparams.nz
            out_channel = self.hparams.ngf*8
            self.conv_rnn_state_sizes.append([self.batch_size, out_channel, conv_rnn_height, conv_rnn_width])
            self.encoder_3_conv = nn.ModuleList()
            self.encoder_3_conv += [nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))]
            self.encoder_3_conv += [nn.AvgPool2d(kernel_size=(2,2), stride=(2,2))]
            self.encoder_3_conv += [nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)]
            self.encoder_3_conv += [nn.ReLU()]
            self.encoder_3_rnn = ConvLSTMCell(
                                input_size=(conv_rnn_height,conv_rnn_width),
                                input_dim=out_channel + self.hparams.nz,
                                hidden_dim=out_channel)
            
            self.cdna_dense_in_channel = out_channel * conv_rnn_height * conv_rnn_width
            
            
            ############### decoder ###############
            self.decoder = nn.ModuleList()
            ### 第 0 层 5/27
            conv_rnn_height, conv_rnn_width = conv_rnn_height*2, conv_rnn_width*2
            in_channel = out_channel + hparams.nz
            out_channel = self.hparams.ngf*8
            self.conv_rnn_state_sizes.append([self.batch_size, out_channel, conv_rnn_height, conv_rnn_width])
            self.decoder_4_conv = nn.ModuleList()
            self.decoder_4_conv += [nn.Upsample(scale_factor=2, mode='bilinear')]
            self.decoder_4_conv += [nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))]
            self.decoder_4_conv += [nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)]
            self.decoder_4_conv += [nn.ReLU()]
            self.decoder_4_rnn = ConvLSTMCell(
                                input_size=(conv_rnn_height,conv_rnn_width),
                                input_dim=out_channel + self.hparams.nz,
                                hidden_dim=out_channel)
            ### 第 1 层 5/27
            conv_rnn_height, conv_rnn_width = conv_rnn_height*2, conv_rnn_width*2
            in_channel = out_channel + hparams.ngf*4 + hparams.nz  ### 与第 2 层级联 5/29
            out_channel = self.hparams.ngf*4
            self.conv_rnn_state_sizes.append([self.batch_size, out_channel, conv_rnn_height, conv_rnn_width])
            self.decoder_5_conv = nn.ModuleList()
            self.decoder_5_conv += [nn.Upsample(scale_factor=2, mode='bilinear')]
            self.decoder_5_conv += [nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))]
            self.decoder_5_conv += [nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)]
            self.decoder_5_conv += [nn.ReLU()]
            self.decoder_5_rnn = ConvLSTMCell(
                                input_size=(conv_rnn_height,conv_rnn_width),
                                input_dim=out_channel + self.hparams.nz,
                                hidden_dim=out_channel)
            ### 第 2 层 5/27
            conv_rnn_height, conv_rnn_width = conv_rnn_height*2, conv_rnn_width*2
            in_channel = out_channel + hparams.ngf*2 + hparams.nz  ### 与第 1 层级联 5/29
            out_channel = self.hparams.ngf*2
            self.conv_rnn_state_sizes.append([self.batch_size, out_channel, conv_rnn_height, conv_rnn_width])
            self.decoder_6_conv = nn.ModuleList()
            self.decoder_6_conv += [nn.Upsample(scale_factor=2, mode='bilinear')]
            self.decoder_6_conv += [nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))]
            self.decoder_6_conv += [nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)]
            self.decoder_6_conv += [nn.ReLU()]
            ### 第 3 层 5/27
            conv_rnn_height, conv_rnn_width = conv_rnn_height*2, conv_rnn_width*2
            in_channel = out_channel + hparams.ngf + hparams.nz  ### 与第 0 层级联 5/29
            out_channel = self.hparams.ngf
            self.conv_rnn_state_sizes.append([self.batch_size, out_channel, conv_rnn_height, conv_rnn_width])
            self.decoder_7_conv = nn.ModuleList()
            self.decoder_7_conv += [nn.Upsample(scale_factor=2, mode='bilinear')]
            self.decoder_7_conv += [nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))]
            self.decoder_7_conv += [nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)]
            self.decoder_7_conv += [nn.ReLU()]
            
        else:
            raise NotImplementedError
        
        ############### cdna kernel ###############
        ### for cdna kernel, 忽略其他transformation 5/27
        self.cdna_kernel_shape = list(self.hparams.kernel_size) + \
                    [self.hparams.last_frames * self.hparams.num_transformed_images]
        self.cdna = Dense(input_shape=[self.batch_size, self.cdna_dense_in_channel],
                          units=torch.prod(torch.tensor(self.cdna_kernel_shape)))### Dense 的 input_shape 要改？ 5/29
        
        ############### scratch_images ###############
        in_channel = out_channel   ### 来自 decoder 最后一层的输出 h 5/30
        out_channel = self.hparams.ngf
        self.scratch_h = nn.ModuleList()
        self.scratch_h += [nn.Conv2d(
                        in_channels=in_channel,
                        out_channels=out_channel,
                        kernel_size=3,
                        stride=1,
                        padding=(1,1))]
        self.scratch_h += [nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)]
        self.scratch_h += [nn.ReLU()]
        
        in_channel = out_channel
        out_channel = color_channel
        self.scratch_img = nn.ModuleList()
        self.scratch_img += [nn.Conv2d(
                        in_channels=in_channel,
                        out_channels=out_channel,
                        kernel_size=3,
                        stride=1,
                        padding=(1,1))]
        self.scratch_img += [nn.Sigmoid()]
        
        ############### masks ###############
        self.num_masks = self.hparams.last_frames * self.hparams.num_transformed_images + \
            int(bool(self.hparams.prev_image_background)) + \
            int(bool(self.hparams.first_image_background and not self.hparams.context_images_background)) + \
            int(bool(self.hparams.last_image_background and not self.hparams.context_images_background)) + \
            int(bool(self.hparams.last_context_image_background and not self.hparams.context_images_background)) + \
            (self.hparams.context_frames if self.hparams.context_images_background else 0) + \
            int(bool(self.hparams.generate_scratch_image))
        
        in_channel = self.hparams.ngf    ### 来自 decoder 最后一层的输出 h 5/30
        out_channel = self.hparams.ngf
        self.masks_h = nn.ModuleList()
        self.masks_h += [nn.Conv2d(
                        in_channels=in_channel,
                        out_channels=out_channel,
                        kernel_size=3,
                        stride=1,
                        padding=(1,1))]
        self.masks_h += [nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)]
        self.masks_h += [nn.ReLU()]
        
        ### 认为 num_masks=num_transformed_images
        in_channel = out_channel + self.num_masks * color_channel ### tf.concat([h_masks] + transformed_images, axis=-1) 5/30
        out_channel = self.num_masks
        self.masks = nn.ModuleList()
        self.masks += [nn.Conv2d(
                        in_channels=in_channel,
                        out_channels=out_channel,
                        kernel_size=3,
                        stride=1,
                        padding=(1,1))]
        self.masks += [nn.Softmax()]
        
        
        
        
    def forward(self, inputs, states, all_images):
        ### inputs = {'images':(NCHW), 'zs':(N,nz),} 5/28
        ### states = {'gen_image':(NCHW), 'last_images':(DNCHW), 'rnn_z_state':(hx,cx), 'conv_rnn_states':, } 5/28
        ### all_images = (DNCHW) 代替原来的 self.inputs['images'] 5/29
        time = states['time']
        conv_rnn_states = states['conv_rnn_states']
        ### 暂时忽略schedule sampling 5/28
        image = inputs['images'] if time<self.hparams.context_frames else states['gen_image']
        last_images = states['last_images'][1:]+[image]  ### 待测试 5/28
        
        ############### latent code ###############
        state_action_z = []
        ### states['rnn_z_state'] 应该是一个 tuple=(hx,cx) 5/28
        #print(inputs['zs'].shape)
        #print(states['rnn_z_state'][0].shape)
        hx, cx = self.lstm_z(inputs['zs'], states['rnn_z_state'])
        rnn_z_state = (hx, cx)  ### hx,cx 对应 rnn_z, rnn_z_state 5/29
        state_action_z = hx    ### state_action_z = (N, hidden_size=hparams.nz) 5/29
        
        ############### encoder ###############
        layers = []
        new_conv_rnn_states = []
        ### 第 0 层 5/29
        ### all_images 代替原来的 self.inputs['images'][0] 5/29
        h = torch.cat([image, all_images[0]], dim=1)  ### h = (N,C*2,H,W) 5/29
        h = util.tile_concat([h, state_action_z[:, :, None, None]], axis=1)  ### h = (N, C*2+hparams.nz, H,W) 5/29
        for layer in self.encoder_0_conv:
            h = layer(h)
        layers.append((h,))   ### h=(N, hparams.hgf, H/2, W/2) 5/29
        ### 第 1 层 5/29
        h = layers[-1][-1]
        h = util.tile_concat([h, state_action_z[:, :, None, None]], dim=1)### h=(N, hparams.rgf+hparams.nz, H/2,W/2) 5/29
        for layer in self.encoder_1_conv:
            h = layer(h)
        conv_rnn_h = util.tile_concat([h, state_action_z[:, :, None, None]], axis=1)
        conv_rnn_state = conv_rnn_states[len(new_conv_rnn_states)]
        conv_rnn_h, conv_rnn_state = self.encoder_1_rnn(conv_rnn_h, conv_rnn_state)  ### 为什么是这样？ 5/29
        new_conv_rnn_states.append(conv_rnn_state)
        layers.append((h, conv_rnn_h))
        ### 第 2 层 5/29
        h = layers[-1][-1]
        h = util.tile_concat([h, state_action_z[:, :, None, None]], dim=1)  ### h=(N,out_channel+hparams.nz,h',w') 5/29
        for layer in self.encoder_2_conv:
            h = layer(h)
        conv_rnn_h = util.tile_concat([h, state_action_z[:, :, None, None]], axis=1)
        conv_rnn_state = conv_rnn_states[len(new_conv_rnn_states)]
        conv_rnn_h, conv_rnn_state = self.encoder_2_rnn(conv_rnn_h, conv_rnn_state)
        new_conv_rnn_states.append(conv_rnn_state)
        layers.append((h, conv_rnn_h))
        ### 第 3 层 5/29
        h = layers[-1][-1]
        h = util.tile_concat([h, state_action_z[:, :, None, None]], dim=1)
        for layer in self.encoder_3_conv:
            h = layer(h)
        conv_rnn_h = util.tile_concat([h, state_action_z[:, :, None, None]], axis=1)
        conv_rnn_state = conv_rnn_states[len(new_conv_rnn_states)]
        conv_rnn_h, conv_rnn_state = self.encoder_3_rnn(conv_rnn_h, conv_rnn_state)
        new_conv_rnn_states.append(conv_rnn_state)
        layers.append((h, conv_rnn_h))
        
        ############### decoder ###############
        ### 第 4 层 5/29
        h = layers[-1][-1]
        h = util.tile_concat([h, state_action_z[:, :, None, None]], dim=1)
        for layer in self.decoder_4_conv:
            h = layer(h)
        conv_rnn_h = util.tile_concat([h, state_action_z[:, :, None, None]], axis=1)
        conv_rnn_state = conv_rnn_states[len(new_conv_rnn_states)]
        conv_rnn_h, conv_rnn_state = self.decoder_4_rnn(conv_rnn_h, conv_rnn_state)
        new_conv_rnn_states.append(conv_rnn_state)
        layers.append((h, conv_rnn_h))
        ### 第 5 层 5/29
        h = torch.cat([layers[-1][-1], layers[2][-1]], dim=1)
        h = util.tile_concat([h, state_action_z[:, :, None, None]], dim=1)
        for layer in self.decoder_5_conv:
            h = layer(h)
        conv_rnn_h = util.tile_concat([h, state_action_z[:, :, None, None]], axis=1)
        conv_rnn_state = conv_rnn_states[len(new_conv_rnn_states)]
        conv_rnn_h, conv_rnn_state = self.decoder_5_rnn(conv_rnn_h, conv_rnn_state)
        new_conv_rnn_states.append(conv_rnn_state)
        layers.append((h, conv_rnn_h))
        ### 第 6 层 5/29
        h = torch.cat([layers[-1][-1], layers[1][-1]], dim=1)
        h = util.tile_concat([h, state_action_z[:, :, None, None]], dim=1)
        for layer in self.decoder_6_conv:
            h = layer(h)
        layers.append((h,))
        ### 第 7 层 5/29
        h = torch.cat([layers[-1][-1], layers[0][-1]], dim=1)
        h = util.tile_concat([h, state_action_z[:, :, None, None]], dim=1)
        for layer in self.decoder_7_conv:
            h = layer(h)
        layers.append((h,))
        assert len(new_conv_rnn_states) == len(conv_rnn_states)
        
        ############### cdna kernel ###############
        smallest_layer = layers[self.num_encoder_layers - 1][-1]
        cdna_kernels = self.cdna(torch.flatten(smallest_layer, start_dim=1, end_dim=-1))
        cdna_kernels = cdna_kernels.reshape([self.batch_size] + self.cdna_kernel_shape)
        cdna_kernels = cdna_kernels + identity_kernel(self.hparams.kernel_size)[None, :, :, None]
        ### cdna_kernels.shape=(batch_size, 5, 5, last_frames * num_transformed_images) 5/30
        
        ############### scratch images ###############
        h_scratch = layers[-1][-1]
        for layer in self.scratch_h:
            h_scrath = layer(h_scratch)
        
        scratch_image = h_scratch
        for layer in self.scratch_img:
            scratch_image = layer(scratch)
        
        ############# transformed images #############
        transformed_images = []
        transformed_images.extend(apply_kernels(last_images, cdna_kernels, self.hparams.dilation_rate))
        if self.hparams.prev_image_background:
            transformed_images.append(image)
        if self.hparams.first_image_background and not self.hparams.context_images_background:
            transformed_images.append(all_images[0])
        if self.hparams.last_image_background and not self.hparams.context_images_background:
            transformed_images.append(all_images[self.hparams.context_frames - 1])
        if self.hparams.context_images_background:
            transformed_images.extend(torch.split(all_images[:self.hparams.context_frames],1,dim=0))
        if self.hparams.generate_scratch_image:
            transformed_images.append(scratch_image)
        
        ################### mask ###################
        h_masks = layers[-1][-1]
        for layer in self.masks_h:
            h_masks = layer(h_masks)
        
        masks = torch.cat([h_masks] + transformed_images, dim=1)  ### channel 维度级联 5/30
        for layer in self.masks:
            masks = layer(masks)
        
        masks = torch.split(masks, len(transformed_images), dim=1)
        
        ############# generate images #############
        assert len(transformed_images) == len(masks)
        gen_image = sum([trans_image * mask for tran_image, mask in zip(transformed_images, masks)])
        
        ################## output ##################
        outputs = {'gen_images': gen_image,
                   'transformed_images': torch.stack(transformed_images, dim=1),
                   'masks': torch.stack(masks, dim=1)} ### dim 取值 -1？ 5/30 
        new_states = {'time': time + 1,
                      'gen_image': gen_image,
                      'last_images': last_images,
                      'conv_rnn_states': new_conv_rnn_states}
        if 'zs' in inputs and self.hparams.use_rnn_z and not self.hparams.ablation_rnn:
            new_states['rnn_z_state'] = rnn_z_state
        
        return outputs, new_states
        

### 编写于 5/23 6/1
class GeneratorGivenZ(nn.Module):
    ### inputs 是一个 dict 5/23
    ### keys() = ['images', 'zs'] 5/23
    def __init__(self, input_shape, mode, hparams):
        super(GeneratorGivenZ, self).__init__()
        self.input_shape = input_shape
        self.image_shape = list(input_shape['images'][2:])
        self.time_length = input_shape['images'][0]
        self.hparams = hparams
        self.mode = mode
        
        savp_input_shape = {}
        savp_input_shape['images'] = list(input_shape['images'][1:])
        savp_input_shape['zs'] = list(input_shape['zs'][1:])
        print(savp_input_shape)
        self.savpcell = SAVPCell(savp_input_shape, mode, hparams)
        
        
    def forward(self, inputs):
        inputs = {name: maybe_pad_or_slice(input, self.hparams.sequence_length - 1)
              for name, input in inputs.items()}
        ### initial state 6/1
        states = {}
        states['time'] = 0
        states['gen_image'] = torch.zeros(self.image_shape)
        states['last_images'] = [inputs['images'][0]] * self.hparams.last_frames
        rnn_z_state_sizes = self.savpcell.rnn_z_state_sizes
        conv_rnn_state_sizes = self.savpcell.conv_rnn_state_sizes
        states['rnn_z_state'] = (torch.zeros(rnn_z_state_sizes),torch.zeros(rnn_z_state_sizes))
        states['conv_rnn_states'] = [(torch.zeros(conv_rnn_state_size),torch.zeros(conv_rnn_state_size))
                                    for conv_rnn_state_size in conv_rnn_state_sizes]
        
        ### 把 savpcell 扩展成 rnn 6/1
        outputs = {}
        for i in range(self.time_length):
            input = {k: v[i] for k, v in inputs.items()}
            output, new_states = self.savpcell(input, states, inputs['images'])
            if i==0:
                outputs = output
            else:
                for k in output.keys():
                    outputs[k] = torch.cat([outputs[k],[output[k]]],dim=0)
            states = new_states
            
        return outputs


### 编写于 5/23
### 待测试。。。 5/23
class Generator(nn.Module):
    ### inputs DNCHW 5/23
    def __init__(self, input_shape, mode, hparams):
        super(Generator, self).__init__()
        self.mode = mode
        self.hparams = hparams
        self.input_shape = input_shape
        self.batch_size = input_shape[1]
        
        self.zs_shape = [hparams.sequence_length - 1, self.batch_size, hparams.nz]
        self.encoder = Posterior(input_shape, hparams)
        if hparams.learn_prior:
            self.prior = Prior(input_shape, hparams)
        else:
            self.prior = None
        posterior_shape = {}
        posterior_shape['images'] = input_shape
        posterior_shape['zs'] = [input_shape[0]-1, input_shape[1], hparams.nz]
        self.generator = GeneratorGivenZ(input_shape=posterior_shape, mode=mode, hparams=hparams)  ### 待完成 5/23
        
    def forward(self, images):
        inputs = {}
        inputs['images'] = images
        if self.hparams.nz == 0:
            outputs = self.generator(inputs)
        else:
            ### encoder 生成 posterior  5/23
            outputs_posterior = self.encoder(inputs['images'])
            #print(outputs_posterior)
            eps = torch.randn(self.zs_shape)
            zs_posterior = outputs_posterior['zs_mu'] + \
                    torch.sqrt(torch.exp(outputs_posterior['zs_log_sigma_sq'])) * eps
            inputs_posterior = inputs
            inputs_posterior['zs'] = zs_posterior
            for k, v in inputs_posterior.items():
                print(k, v.shape)
            print('-'*20)
            
            ### 生成 prior 5/23
            if self.hparams.learn_prior:
                outputs_prior = self.prior(inputs['images'])
                eps = torch.randn(self.zs_shape)
                zs_prior = outputs_prior['zs_mu'] + \
                    torch.sqrt(torch.exp(outputs_prior['zs_log_sigma_sq'])) * eps
            else:
                outputs_prior = {}
                zs_prior = torch.randn([self.hparams.sequence_length - self.hparams.context_frames] + \
                                       self.zs_shape[1:])
                zs_prior = torch.cat([zs_posterior[:self.hparams.context_frames - 1], zs_prior], dim=0)
            inputs_prior = inputs
            inputs_prior['zs'] = zs_prior
            for k, v in inputs_prior.items():
                print(k, v.shape)
            print('-'*20)
            
            ### posterior 和 images 交给 generator 5/23
            gen_outputs_posterior = self.generator(inputs_posterior)
            gen_outputs = self.generator(inputs_prior)
            
            # rename tensors to avoid name collisions
            output_prior = collections.OrderedDict([(k + '_prior', v) for k, v in outputs_prior.items()])
            outputs_posterior = collections.OrderedDict([(k + '_enc', v) for k, v in outputs_posterior.items()])
            gen_outputs_posterior = collections.OrderedDict([(k + '_enc', v) for k, v in gen_outputs_posterior.items()])
            
            outputs = [output_prior, gen_outputs, outputs_posterior, gen_outputs_posterior]
            total_num_outputs = sum([len(output) for output in outputs])
            ### 整合 5/23
            outputs = collections.OrderedDict(itertools.chain(*[output.items() for output in outputs]))
            assert len(outputs) == total_num_outputs  # ensure no output is lost because of repeated keys

            ### 根据 prior 生成多个随机抽样 5/23
            ### num_samples 是采样的个数 5/23
            inputs_samples = {
                name: torch.repeat(input[:, None], [1, self.hparams.num_samples]+[1]*(input.shape.ndims - 1))
                for name, input in inputs.items()}
            zs_samples_shape = [self.hparams.sequence_length - 1,
                                self.hparams.num_samples,
                                self.batch_size,
                                self.hparams.nz]
            if self.hparam.learn_prior:
                eps = torch.randn(zs_samples_shape)
                zs_prior_samples = (outputs_prior['zs_mu'][:, None] +
                                torch.sqrt(torch.exp(outputs_prior['zs_log_sigma_sq']))[:, None] * eps)
            else:
                zs_prior_samples = torch.randn(
                    [self.hparams.sequence_length - self.hparams.context_frames] + zs_samples_shape[1:])
                zs_prior_samples = torch.cat(
                    [torch.repeat(zs_posterior[:self.hparams.context_frames - 1][:, None],
                                  [1, self.hparams.num_samples, 1, 1]),
                     zs_prior_samples], dim=0)
            inputs_prior_samples = dict(inputs_samples)
            inputs_prior_samples['zs'] = zs_prior_samples
            
            ### 第1，2个维度压平 5/23
            inputs_prior_samples = {name: torch.flatten(input, start_dim=1, end_dim=2)
                                    for name, input in inputs_prior_samples.items()}
            gen_outputs_samples = self.generator(inputs_prior_samples)
            gen_images_samples = gen_outputs_samples['gen_images']
            ### 再恢复出前两个维度 5/23
            gen_images_samples = torch.stack(gen_images_samples.chunk(self.hparams.num_samples, dim=1), dim=-1)
            gen_images_samples_avg = torch.mean(gen_images_samples, dim=-1)
            outputs['gen_images_samples'] = gen_images_samples
            outputs['gen_images_samples_avg'] = gen_images_samples_avg
        
        return outputs
        
        
### 5/30
def identity_kernel(kernel_size):
    ### kernel 中心为1或0.25，其余为0，有什么用？ 5/19
    kh, kw = kernel_size
    kernel = np.zeros(kernel_size)

    def center_slice(k):
        if k % 2 == 0:
            ### 只有最中心的4个点为0.25 5/26
            return slice(k // 2 - 1, k // 2 + 1)
        else:
            ### 只有最中心的一个点 k//2 处为1.0 5/26
            return slice(k // 2, k // 2 + 1)

    kernel[center_slice(kh), center_slice(kw)] = 1.0
    kernel /= np.sum(kernel)
    return kernel
        
def apply_kernels(image, kernels, dilation_rate=(1, 1)):
    """
    Args:
        image: A 4-D tensor of shape
            `[batch, in_height, in_width, in_channels]`.
        kernels: A 4-D tensor of shape
            `[batch, kernel_size[0], kernel_size[1], num_transformed_images]` or

    Returns:
        A list of `num_transformed_images` 4-D tensors, each of shape
            `[batch, in_height, in_width, in_channels]`.
    """
    if isinstance(image, list):
        image_list = image
        kernels_list = torch.chunk(kernels, len(image_list), dim=-1)
        outputs = []
        for image, kernels in zip(image_list, kernels_list):
            outputs.extend(apply_kernels(image, kernels))
    else:
        outputs = apply_cdna_kernels(image, kernels, dilation_rate=dilation_rate)
    return outputs

### 待测试。。。5/30
def apply_cdna_kernels(images, kernels, dilation_rate=(1, 1)):
    """
    Args:
        image: A 4-D tensor of shape
            `[batch, in_height, in_width, in_channels]`.
        kernels: A 4-D of shape
            `[batch, kernel_size[0], kernel_size[1], num_transformed_images]`.

    Returns:
        A list of `num_transformed_images` 4-D tensors, each of shape
            `[batch, in_height, in_width, in_channels]`.
    """
    batch_size, height, width, color_channels = images.shape
    batch_size, kernel_height, kernel_width, num_transformed_images = kernels.shape
    images = images.permute([3, 0, 1, 2])
    kernels = kernels.permute([3, 0, 1, 2])
    images = torch.chunk(images, batch_size, dim=1)
    kernels = torch.chunk(kernels, batch_size, dim=1)
    outputs = []
    for img, k in zip(images, kernels):
        ### 将 color_channel 看作 batch_size，因为对不同的通道，transform是相同的 5/30
        ### 对每个 sample 应用不同的 kernel 5/30
        ### img = C1HW
        ### K = C'1hw
        ### output = CC'HW
        output = F.conv2d(input=img, weight=k, padding=2) ### kernel 为 5*5 5/30
        outputs.append(output)
    ### outputs = NCC'HW 5/30
    outputs = torch.cat(outputs, dim=0)
    outputs = outputs.reshape([batch_size, color_channels, num_transformed_images, height, width])
    outputs = outputs.permute([2, 0, 1, 3, 4])  ### C'NCHW 5/30
    outputs = torch.split(outputs, 1, dim=0)
    return outputs
    

    

### 融合BaseVideoPredModel 和 VideoPredModel 5/15
class BaseVideoPredictionModel(nn.Module):
    def __init__(self, mode='train', hparams_dict=None, hparams=None,
                 num_gpus=None, eval_num_samples=100,
                 eval_num_samples_for_diversity=10, eval_parallel_iterations=1,
                 aggregate_nccl=False,
                 **kwargs):
        """
        Base video prediction model.

        Trainable and non-trainable video prediction models can be derived
        from this base class.

        Args:
            mode: `'train'` or `'test'`.
            hparams_dict: a dict of `name=value` pairs, where `name` must be
                defined in `self.get_default_hparams()`.
            hparams: a string of comma separated list of `name=value` pairs,
                where `name` must be defined in `self.get_default_hparams()`.
                These values overrides any values in hparams_dict (if any).
        """
        super(BaseVideoPredictionModel, self).__init__()
        if mode not in ('train', 'test'):
            raise ValueError('mode must be train or test, but %s given' % mode)
        self.mode = mode
        cuda_visible_devices = os.environ.get('CUDA_VISIBLE_DEVICES', '0')
        if cuda_visible_devices == '':
            max_num_gpus = 0
        else:
            max_num_gpus = len(cuda_visible_devices.split(','))
        if num_gpus is None:
            num_gpus = max_num_gpus
        elif num_gpus > max_num_gpus:
            raise ValueError('num_gpus=%d is greater than the number of visible devices %d' % (num_gpus, max_num_gpus))
        self.num_gpus = num_gpus
        self.eval_num_samples = eval_num_samples
        self.eval_num_samples_for_diversity = eval_num_samples_for_diversity
        self.eval_parallel_iterations = eval_parallel_iterations
        self.hparams = self.parse_hparams(hparams_dict, hparams)
        if self.hparams.context_frames == -1:
            raise ValueError('Invalid context_frames %r. It might have to be '
                             'specified.' % self.hparams.context_frames)
        if self.hparams.sequence_length == -1:
            raise ValueError('Invalid sequence_length %r. It might have to be '
                             'specified.' % self.hparams.sequence_length)
        
        # should be overriden by descendant class if the model is stochastic
        self.deterministic = True

        # member variables that should be set by `self.build_graph`
        self.inputs = None
        self.gen_images = None
        self.output = None
        self.metrics = None
        self.eval_output = None
        self.eval_metrics = None
        self.accum_eval_metrics = None
        self.saveable_variables = None
        self.post_init_ops = None
        
        self.generator = Generator(mode=self.mode, hparams=self.hparams)
        if self.discrm:
            self.discriminator = VideoDiscriminator(discriminator_fn, mode=self.mode, hparams=self.hparams)
        else:
            self.discriminator = None
        self.aggregate_nccl = aggregate_nccl
        
        ### 改自savp_model.py SAVPCell 5/16
        self.encoder0 = Encoder
        
    ### inputs.shape=()? 5/15
    def forward(self, inputs):
        outputs = {}
        output = self.generator(inputs)
        outputs['gen_output'] = output
        if self.discrim:
            output = self.discriminator_fn(inputs, output)
            outputs['discrim_output'] = output
        
        return outputs
        
        
    def get_default_hparams_dict(self):
        """
        The keys of this dict define valid hyperparameters for instances of
        this class. A class inheriting from this one should override this
        method if it has a different set of hyperparameters.

        Returns:
            A dict with the following hyperparameters.

            context_frames: the number of ground-truth frames to pass in at
                start. Must be specified during instantiation.
            sequence_length: the number of frames in the video sequence,
                including the context frames, so this model predicts
                `sequence_length - context_frames` future frames. Must be
                specified during instantiation.
            repeat: the number of repeat actions (if applicable).
        """
        hparams = dict(
            context_frames=-1,
            sequence_length=-1,
            repeat=1,
            batch_size=16,
            lr=0.001,
            end_lr=0.0,
            decay_steps=(200000, 300000),   ### 学习率衰减，在train的时候设置，待定 5/15
            lr_boundaries=(0,),
            max_steps=300000,
            beta1=0.9,
            beta2=0.999,
            clip_length=10,
            l1_weight=0.0,
            l2_weight=1.0,
            vgg_cdist_weight=0.0,
            feature_l2_weight=0.0,
            ae_l2_weight=0.0,
            state_weight=0.0,
            tv_weight=0.0,
            image_sn_gan_weight=0.0,
            image_sn_vae_gan_weight=0.0,
            images_sn_gan_weight=0.0,
            images_sn_vae_gan_weight=0.0,
            video_sn_gan_weight=0.0,
            video_sn_vae_gan_weight=0.0,
            gan_feature_l2_weight=0.0,
            gan_feature_cdist_weight=0.0,
            vae_gan_feature_l2_weight=0.0,
            vae_gan_feature_cdist_weight=0.0,
            gan_loss_type='LSGAN',
            joint_gan_optimization=False,
            kl_weight=0.0,
            kl_anneal='linear',
            kl_anneal_k=-1.0,
            kl_anneal_steps=(50000, 100000),
            z_l1_weight=0.0,
        )
        return hparams
    
    def get_default_hparams(self):
        return HParams(**self.get_default_hparams_dict())
    
    def parse_hparams(self, hparams_dict, hparams):
        parsed_hparams = self.get_default_hparams().override_from_dict(hparams_dict or {})
        if hparams:
            if not isinstance(hparams, (list, tuple)):
                hparams = [hparams]
            for hparam in hparams:
                parsed_hparams.parse(hparam)
        return parsed_hparams
    
    