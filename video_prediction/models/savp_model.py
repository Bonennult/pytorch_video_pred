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


class SAVPCell(nn.Module):
    ### 假定 input_shape={'images':NCHW, 'zs':(N,nz)} 5/27
    ### 假定这里的 images 和 zs 都是经过 unroll_rnn 拆分过的 5/27
    def __init__(self, input_shape, mode, hparams):
        super(SAVPCell, self).__init__()
        self.mode = mode
        self.hparams = hparams
        self.input_shape = input_shape  ### 设定为一个dict 5/23
        self.batch_size = input_shape['images'].shape[0]   ### images.shape=NCHW 5/23
        self.image_shape = list(input_shape['images'].shape[-3:])
        channel, height, width = self.image_shape
        self.num_encoder_layers = 4
        self.time = 0
        
        ### LSTMCell inputs 应当是 (batch_size,input_size) 5/28
        self.lstm_z = nn.LSTMCell(input_size=self.input_shape['zs'].shape[1], hidden_size=self.hparams.nz)
        
        self.scale_size = min(height, width)
        if self.scale_size >= 256:
            raise NotImplementedError
        elif self.scale_size >=128:
            ### encoder 5/26
            ### savp_model #523 5/26
            conv_rnn_height, conv_rnn_width = height, width
            ### 第 0 层 5/26
            ### conv_pool + norm + activation 5/26
            conv_rnn_height, conv_rnn_width = conv_rnn_height//2, conv_rnn_width//2
            in_channel = self.input_shape['imaegs'][1]*2 + self.hparams.nz
            out_channel = self.hparams.ngf
            self.encoder_0_conv = nn.ModuleList()
            self.encoder_0_conv += nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=5,
                                stride=1,
                                padding=(2,2))
            self.encoder_0_conv += nn.AvgPool2d(kernel_size=(2,2), stride=(2,2))
            self.encoder_0_conv += nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)
            self.encoder_0_conv += nn.ReLU()
            ### 第 1 层 5/26
            conv_rnn_height, conv_rnn_width = conv_rnn_height//2, conv_rnn_width//2
            in_channel = out_channel + hparams.nz
            out_channel = self.hparams.ngf*2
            self.encoder_1_conv = nn.ModuleList()
            self.encoder_1_conv += nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))
            self.encoder_1_conv += nn.AvgPool2d(kernel_size=(2,2), stride=(2,2))
            self.encoder_1_conv += nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)
            self.encoder_1_conv += nn.ReLU()
            self.encoder_1_rnn += ConvLSTMCell(
                                input_size=(conv_rnn_height,conv_rnn_width),
                                input_dim=out_channel + self.hparams.nz,
                                hidden_dim=out_channel)
            ### 第 2 层 5/26
            conv_rnn_height, conv_rnn_width = conv_rnn_height//2, conv_rnn_width//2
            in_channel = out_channel + hparams.nz
            out_channel = self.hparams.ngf*4
            self.encoder_2_conv = nn.ModuleList()
            self.encoder_2_conv += nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))
            self.encoder_2_conv += nn.AvgPool2d(kernel_size=(2,2), stride=(2,2))
            self.encoder_2_conv += nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)
            self.encoder_2_conv += nn.ReLU()
            self.encoder_2_rnn += ConvLSTMCell(
                                input_size=(conv_rnn_height,conv_rnn_width),
                                input_dim=out_channel + self.hparams.nz,
                                hidden_dim=out_channel)
            ### 第 3 层 5/26
            conv_rnn_height, conv_rnn_width = conv_rnn_height//2, conv_rnn_width//2
            in_channel = out_channel + hparams.nz
            out_channel = self.hparams.ngf*8
            self.encoder_3_conv = nn.ModuleList()
            self.encoder_3_conv += nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))
            self.encoder_3_conv += nn.AvgPool2d(kernel_size=(2,2), stride=(2,2))
            self.encoder_3_conv += nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)
            self.encoder_3_conv += nn.ReLU()
            self.encoder_3_rnn += ConvLSTMCell(
                                input_size=(conv_rnn_height,conv_rnn_width),
                                input_dim=out_channel + self.hparams.nz,
                                hidden_dim=out_channel)
            
            self.cdna_dense_in_channel = out_channel * conv_rnn_height * conv_rnn_width
            
            
            ### decoder 5/26
            self.decoder = nn.ModuleList()
            ### 第 0 层 5/27
            conv_rnn_height, conv_rnn_width = conv_rnn_height*2, conv_rnn_width*2
            in_channel = out_channel + hparams.nz
            out_channel = self.hparams.ngf*8
            self.decoder_4_conv = nn.ModuleList()
            self.decoder_4_conv += nn.Upsample(scale_factor=2, mode='bilinear')
            self.decoder_4_conv += nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))
            self.decoder_4_conv += nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)
            self.decoder_4_conv += nn.ReLU()
            self.decoder_4_rnn += ConvLSTMCell(
                                input_size=(conv_rnn_height,conv_rnn_width),
                                input_dim=out_channel + self.hparams.nz,
                                hidden_dim=out_channel)
            ### 第 1 层 5/27
            conv_rnn_height, conv_rnn_width = conv_rnn_height*2, conv_rnn_width*2
            in_channel = out_channel + hparams.ngf*4 + hparams.nz  ### 与第 2 层级联 5/29
            out_channel = self.hparams.ngf*4
            self.decoder_5_conv = nn.ModuleList()
            self.decoder_5_conv += nn.Upsample(scale_factor=2, mode='bilinear')
            self.decoder_5_conv += nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))
            self.decoder_5_conv += nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)
            self.decoder_5_conv += nn.ReLU()
            self.decoder_5_rnn += ConvLSTMCell(
                                input_size=(conv_rnn_height,conv_rnn_width),
                                input_dim=out_channel + self.hparams.nz,
                                hidden_dim=out_channel)
            ### 第 2 层 5/27
            conv_rnn_height, conv_rnn_width = conv_rnn_height*2, conv_rnn_width*2
            in_channel = out_channel + hparams.ngf*2 + hparams.nz  ### 与第 1 层级联 5/29
            out_channel = self.hparams.ngf*2
            self.decoder_6_conv = nn.ModuleList()
            self.decoder_6_conv += nn.Upsample(scale_factor=2, mode='bilinear')
            self.decoder_6_conv += nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))
            self.decoder_6_conv += nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)
            self.decoder_6_conv += nn.ReLU()
            ### 第 3 层 5/27
            conv_rnn_height, conv_rnn_width = conv_rnn_height*2, conv_rnn_width*2
            in_channel = out_channel + hparams.ngf + hparams.nz  ### 与第 0 层级联 5/29
            out_channel = self.hparams.ngf
            self.decoder_7_conv = nn.ModuleList()
            self.decoder_7_conv += nn.Upsample(scale_factor=2, mode='bilinear')
            self.decoder_7_conv += nn.Conv2d(
                                in_channels=in_channel,
                                out_channels=out_channel,
                                kernel_size=3,
                                stride=1,
                                padding=(1,1))
            self.decoder_7_conv += nn.InstanceNorm2d(num_features=out_channel, eps=1e-6)
            self.decoder_7_conv += nn.ReLU()
            
        else:
            raise NotImplementedError
        
        ### for cdna kernel, 忽略其他transformation 5/27
        self.cdna_kernel_shape = list(self.hparams.kernel_size) + \
                    [self.hparams.last_frames * self.hparams.num_transformed_images]
        self.cdna = Dense(input_shape=[self.batch_size, self.cdna_dense_in_channel],
                          units=torch.prod(self.cdna_kernel_shape))  ### Dense 的 input_shape 要改？ 5/29
        
        ### scratch_images 5/27
        self.scratch_h = nn.ModuleList()
        self.scratch_h += nn.Conv2d()
        self.scratch_h += nn.InstanceNorm2d()
        self.scratch_h += nn.ReLU()
        
        self.scratch_img = nn.ModuleList()
        self.scratch_img += nn.Conv2d()
        self.scratch_img += nn.Sigmoid()
        
        ### masks 5/27
        self.masks_h = nn.ModuleList()
        self.masks_h += nn.Conv2d()
        self.masks_h += nn.InstanceNorm2d()
        self.masks_h += nn.ReLU()
        
        self.masks = nn.ModuleList()
        self.masks += nn.Conv2d()
        self.masks += nn.Softmax()
            
        
        self.num_masks = self.hparams.last_frames * self.hparams.num_transformed_images + \
            int(bool(self.hparams.prev_image_background)) + \
            int(bool(self.hparams.first_image_background and not self.hparams.context_images_background)) + \
            int(bool(self.hparams.last_image_background and not self.hparams.context_images_background)) + \
            int(bool(self.hparams.last_context_image_background and not self.hparams.context_images_background)) + \
            (self.hparams.context_frames if self.hparams.context_images_background else 0) + \
            int(bool(self.hparams.generate_scratch_image))
        
        
    def forward(inputs, states, all_images):
        ### inputs = {'images':(NCHW), 'zs':(N,nz),} 5/28
        ### states = {'gen_image':(NCHW), 'last_images':(DNCHW), 'rnn_z_state':(hx,cx), 'conv_rnn_states':, } 5/28
        ### all_images = (DNCHW) 代替原来的 self.inputs['images'] 5/29
        t = self.time
        conv_rnn_states = states['conv_rnn_states']
        ### 暂时忽略schedule sampling 5/28
        image = inputs['images'] if t<self.hparams.context_frames else states['gen_image']
        last_images = states['last_images'][1:]+[image]  ### 待测试 5/28
        
        ############### latent code ###############
        state_action_z = []
        ### states['rnn_z_state'] 应该是一个 tuple=(hx,cx) 5/28
        hx, cx = self.lstm_z(inputs['zs'], states['rnn_z_state'])
        rnn_z_state = (hx, cx)  ### hx,cx 对应 rnn_z, rnn_z_state 5/29
        state_action_z = hx    ### state_action_z = (N, hidden_size=hparams.nz) 5/29
        
        ############### encoder ###############
        layers = []
        new_conv_rnn_states = []
        ### 第 0 层 5/29
        ### all_images 代替原来的 self.inputs['images'][0] 5/29
        h = torch.cat([image, all_images[0]], dim=1)  ### h = (N,C*2,H,W) 5/29
        h = util.tile_concat([h, state_action_z[:, :, None, None]], dim=1)  ### h = (N, C*2+hparams.nz, H,W) 5/29
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
        
        
        
        


### 融合BaseVideoPredModel 和 VideoPredModel 5/15
class BaseVideoPredictionModel(nn.Module):
    def __init__(self, mode='train', hparams_dict=None, hparams=None,
                 num_gpus=None, eval_num_samples=100,
                 eval_num_samples_for_diversity=10, eval_parallel_iterations=1,
                 aggregate_nccl=False,
                 hparams_dict=None,
                 hparams=None,
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
    
    