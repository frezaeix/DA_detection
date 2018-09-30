# --------------------------------------------------------
# Tensorflow Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Xinlei Chen
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

import utils.timer

from layer_utils.snippets import generate_anchors_pre
from layer_utils.proposal_layer import proposal_layer
from layer_utils.proposal_top_layer import proposal_top_layer
from layer_utils.anchor_target_layer import anchor_target_layer
from layer_utils.proposal_target_layer import proposal_target_layer
from utils.visualization import draw_bounding_boxes

from layer_utils.roi_pooling.roi_pool import RoIPoolFunction
from layer_utils.roi_align.crop_and_resize import CropAndResizeFunction

from model.config import cfg

import tensorboardX as tb

from scipy.misc import imresize

from nets.discriminator_inst import FCDiscriminator_inst
from nets.discriminator_img import FCDiscriminator_img
from nets.decoder import decoder
import cv2
from model.bbox_transform import bbox_transform_inv
class DiffLoss(nn.Module):

    def __init__(self):
        super(DiffLoss, self).__init__()

    def forward(self, input1, input2):

        batch_size = input1.size(0)
        input1 = input1.view(batch_size, -1)
        input2 = input2.view(batch_size, -1)

        input1_l2_norm = torch.norm(input1, p=2, dim=1, keepdim=True).detach()
        input1_l2 = input1.div(input1_l2_norm.expand_as(input1) + 1e-6)

        input2_l2_norm = torch.norm(input2, p=2, dim=1, keepdim=True).detach()
        input2_l2 = input2.div(input2_l2_norm.expand_as(input2) + 1e-6)

        diff_loss = torch.mean((input1_l2.mm(input2_l2.t()).pow(2)))

        return diff_loss

class GradReverse(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        return x.view_as(x)

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output.neg()

def grad_reverse(x):
    return GradReverse.apply(x)

def printgradnorm(self, grad_input, grad_output):
    #print('Inside ' + self.__class__.__name__ + ' backward')
    #print('Inside class:' + self.__class__.__name__)
    #print('')
    #print('grad_input: ', type(grad_input))
    #print('grad_input[0]: ', type(grad_input[0]))
    #print('grad_output: ', type(grad_output))
    #print('grad_output[0]: ', type(grad_output[0]))
    #print('')
    #print('grad_input size:', grad_input[0].size())
    #print('grad_output size:', grad_output[0].size())
    print('grad_input norm:', grad_input[0].data.norm())

class Network(nn.Module):
  def __init__(self):
    nn.Module.__init__(self)
    self._predictions = {}
    self._losses = {}
    self._anchor_targets = {}
    self._proposal_targets = {}
    self._layers = {}
    self._gt_image = None
    self._act_summaries = {}
    self._score_summaries = {}
    self._event_summaries = {}
    self._image_gt_summaries = {}
    self._variables_to_fix = {}

  def _add_gt_image(self):
    # add back mean
    image = self._image_gt_summaries['image'] + cfg.PIXEL_MEANS
    image = imresize(image[0], self._im_info[:2] / self._im_info[2])
    # BGR to RGB (opencv uses BGR)
    self._gt_image = image[np.newaxis, :,:,::-1].copy(order='C')

  def _add_gt_image_summary(self):
    # use a customized visualization function to visualize the boxes
    self._add_gt_image()
    image = draw_bounding_boxes(\
                      self._gt_image, self._image_gt_summaries['gt_boxes'], self._image_gt_summaries['im_info'])

    return tb.summary.image('GROUND_TRUTH', image[0].astype('float32')/255.0)

  def _add_act_summary(self, key, tensor):
    return tb.summary.histogram('ACT/' + key + '/activations', tensor.data.cpu().numpy(), bins='auto'),
    tb.summary.scalar('ACT/' + key + '/zero_fraction',
                      (tensor.data == 0).float().sum() / tensor.numel())

  def _add_score_summary(self, key, tensor):
    return tb.summary.histogram('SCORE/' + key + '/scores', tensor.data.cpu().numpy(), bins='auto')

  def _add_train_summary(self, key, var):
    return tb.summary.histogram('TRAIN/' + key, var.data.cpu().numpy(), bins='auto')

  def _proposal_top_layer(self, rpn_cls_prob, rpn_bbox_pred):
    rois, rpn_scores = proposal_top_layer(\
                                    rpn_cls_prob, rpn_bbox_pred, self._im_info,
                                     self._feat_stride, self._anchors, self._num_anchors)
    return rois, rpn_scores

  def _proposal_layer(self, rpn_cls_prob, rpn_bbox_pred):
    rois, rpn_scores = proposal_layer(\
                                    rpn_cls_prob, rpn_bbox_pred, self._im_info, self._mode,
                                     self._feat_stride, self._anchors, self._num_anchors)

    return rois, rpn_scores

  def _roi_pool_layer(self, bottom, rois):
    return RoIPoolFunction(cfg.POOLING_SIZE, cfg.POOLING_SIZE, 1. / 16.)(bottom, rois)

  def _crop_pool_layer(self, bottom, rois, max_pool=True):
    # implement it using stn
    # box to affine
    # input (x1,y1,x2,y2)
    """
    [  x2-x1             x1 + x2 - W + 1  ]
    [  -----      0      ---------------  ]
    [  W - 1                  W - 1       ]
    [                                     ]
    [           y2-y1    y1 + y2 - H + 1  ]
    [    0      -----    ---------------  ]
    [           H - 1         H - 1      ]
    """
    rois = rois.detach()

    x1 = rois[:, 1::4] / 16.0
    y1 = rois[:, 2::4] / 16.0
    x2 = rois[:, 3::4] / 16.0
    y2 = rois[:, 4::4] / 16.0

    height = bottom.size(2)
    width = bottom.size(3)

    pre_pool_size = cfg.POOLING_SIZE * 2 if max_pool else cfg.POOLING_SIZE
    crops = CropAndResizeFunction(pre_pool_size, pre_pool_size)(bottom, 
      torch.cat([y1/(height-1),x1/(width-1),y2/(height-1),x2/(width-1)], 1), rois[:, 0].int())
    if max_pool:
      crops = F.max_pool2d(crops, 2, 2)
    return crops

  def _anchor_target_layer(self, rpn_cls_score):
    rpn_labels, rpn_bbox_targets, rpn_bbox_inside_weights, rpn_bbox_outside_weights = \
      anchor_target_layer(
      rpn_cls_score.data, self._gt_boxes.data.cpu().numpy(), self._im_info, self._feat_stride, self._anchors.data.cpu().numpy(), self._num_anchors)

    rpn_labels = Variable(torch.from_numpy(rpn_labels).float().cuda()) #.set_shape([1, 1, None, None])
    rpn_bbox_targets = Variable(torch.from_numpy(rpn_bbox_targets).float().cuda())#.set_shape([1, None, None, self._num_anchors * 4])
    rpn_bbox_inside_weights = Variable(torch.from_numpy(rpn_bbox_inside_weights).float().cuda())#.set_shape([1, None, None, self._num_anchors * 4])
    rpn_bbox_outside_weights = Variable(torch.from_numpy(rpn_bbox_outside_weights).float().cuda())#.set_shape([1, None, None, self._num_anchors * 4])

    rpn_labels = rpn_labels.long()
    self._anchor_targets['rpn_labels'] = rpn_labels
    self._anchor_targets['rpn_bbox_targets'] = rpn_bbox_targets
    self._anchor_targets['rpn_bbox_inside_weights'] = rpn_bbox_inside_weights
    self._anchor_targets['rpn_bbox_outside_weights'] = rpn_bbox_outside_weights

    for k in self._anchor_targets.keys():
      self._score_summaries[k] = self._anchor_targets[k]

    return rpn_labels

  def _proposal_target_layer(self, rois, roi_scores):
    rois, roi_scores, labels, bbox_targets, bbox_inside_weights, bbox_outside_weights = \
      proposal_target_layer(
      rois, roi_scores, self._gt_boxes, self._num_classes)

    self._proposal_targets['rois'] = rois
    self._proposal_targets['labels'] = labels.long()
    self._proposal_targets['bbox_targets'] = bbox_targets
    self._proposal_targets['bbox_inside_weights'] = bbox_inside_weights
    self._proposal_targets['bbox_outside_weights'] = bbox_outside_weights

    for k in self._proposal_targets.keys():
      self._score_summaries[k] = self._proposal_targets[k]

    return rois, roi_scores

  def _anchor_component(self, height, width):
    # just to get the shape right
    #height = int(math.ceil(self._im_info.data[0, 0] / self._feat_stride[0]))
    #width = int(math.ceil(self._im_info.data[0, 1] / self._feat_stride[0]))
    anchors, anchor_length = generate_anchors_pre(\
                                          height, width,
                                           self._feat_stride, self._anchor_scales, self._anchor_ratios)
    self._anchors = Variable(torch.from_numpy(anchors).cuda())
    self._anchor_length = anchor_length

  def _smooth_l1_loss(self, bbox_pred, bbox_targets, bbox_inside_weights, bbox_outside_weights, sigma=1.0, dim=[1]):
    sigma_2 = sigma ** 2
    box_diff = bbox_pred - bbox_targets
    in_box_diff = bbox_inside_weights * box_diff
    abs_in_box_diff = torch.abs(in_box_diff)
    smoothL1_sign = (abs_in_box_diff < 1. / sigma_2).detach().float()
    in_loss_box = torch.pow(in_box_diff, 2) * (sigma_2 / 2.) * smoothL1_sign \
                  + (abs_in_box_diff - (0.5 / sigma_2)) * (1. - smoothL1_sign)
    out_loss_box = bbox_outside_weights * in_loss_box
    loss_box = out_loss_box
    for i in sorted(dim, reverse=True):
      loss_box = loss_box.sum(i)
    loss_box = loss_box.mean()
    return loss_box

  def _add_losses(self, sigma_rpn=3.0):
    # RPN, class loss
    rpn_cls_score = self._predictions['rpn_cls_score_reshape'].view(-1, 2)
    rpn_label = self._anchor_targets['rpn_labels'].view(-1)
    rpn_select = Variable((rpn_label.data != -1).nonzero().view(-1))
    rpn_cls_score = rpn_cls_score.index_select(0, rpn_select).contiguous().view(-1, 2)
    rpn_label = rpn_label.index_select(0, rpn_select).contiguous().view(-1)
    rpn_cross_entropy = F.cross_entropy(rpn_cls_score, rpn_label)

    # RPN, bbox loss
    rpn_bbox_pred = self._predictions['rpn_bbox_pred']
    rpn_bbox_targets = self._anchor_targets['rpn_bbox_targets']
    rpn_bbox_inside_weights = self._anchor_targets['rpn_bbox_inside_weights']
    rpn_bbox_outside_weights = self._anchor_targets['rpn_bbox_outside_weights']
    rpn_loss_box = self._smooth_l1_loss(rpn_bbox_pred, rpn_bbox_targets, rpn_bbox_inside_weights,
                                          rpn_bbox_outside_weights, sigma=sigma_rpn, dim=[1, 2, 3])

    # RCNN, class loss
    cls_score = self._predictions["cls_score"]
    label = self._proposal_targets["labels"].view(-1)
    cross_entropy = F.cross_entropy(cls_score.view(-1, self._num_classes), label)

    # RCNN, bbox loss
    bbox_pred = self._predictions['bbox_pred']
    bbox_targets = self._proposal_targets['bbox_targets']
    bbox_inside_weights = self._proposal_targets['bbox_inside_weights']
    bbox_outside_weights = self._proposal_targets['bbox_outside_weights']
    loss_box = self._smooth_l1_loss(bbox_pred, bbox_targets, bbox_inside_weights, bbox_outside_weights)

    self._losses['cross_entropy'] = cross_entropy
    self._losses['loss_box'] = loss_box
    self._losses['rpn_cross_entropy'] = rpn_cross_entropy
    self._losses['rpn_loss_box'] = rpn_loss_box

    loss = cross_entropy + loss_box + rpn_cross_entropy + rpn_loss_box
    self._losses['total_loss'] = loss

    for k in self._losses.keys():
      self._event_summaries[k] = self._losses[k]

    return loss

  def _region_proposal(self, net_conv):
    rpn = F.relu(self.rpn_net(net_conv))
    self._act_summaries['rpn'] = rpn

    rpn_cls_score = self.rpn_cls_score_net(rpn) # batch * (num_anchors * 2) * h * w

    # change it so that the score has 2 as its channel size
    rpn_cls_score_reshape = rpn_cls_score.view(1, 2, -1, rpn_cls_score.size()[-1]) # batch * 2 * (num_anchors*h) * w
    rpn_cls_prob_reshape = F.softmax(rpn_cls_score_reshape, dim=1)
    
    # Move channel to the last dimenstion, to fit the input of python functions
    rpn_cls_prob = rpn_cls_prob_reshape.view_as(rpn_cls_score).permute(0, 2, 3, 1) # batch * h * w * (num_anchors * 2)
    rpn_cls_score = rpn_cls_score.permute(0, 2, 3, 1) # batch * h * w * (num_anchors * 2)
    rpn_cls_score_reshape = rpn_cls_score_reshape.permute(0, 2, 3, 1).contiguous()  # batch * (num_anchors*h) * w * 2
    rpn_cls_pred = torch.max(rpn_cls_score_reshape.view(-1, 2), 1)[1]

    rpn_bbox_pred = self.rpn_bbox_pred_net(rpn)
    rpn_bbox_pred = rpn_bbox_pred.permute(0, 2, 3, 1).contiguous()  # batch * h * w * (num_anchors*4)

    if self._mode == 'TRAIN':
      rois, roi_scores = self._proposal_layer(rpn_cls_prob, rpn_bbox_pred) # rois, roi_scores are varible ##error
      rpn_labels = self._anchor_target_layer(rpn_cls_score)
      rois, _ = self._proposal_target_layer(rois, roi_scores)
    else:
      if cfg.TEST.MODE == 'nms':
        rois, self.roi_scores = self._proposal_layer(rpn_cls_prob, rpn_bbox_pred)
      elif cfg.TEST.MODE == 'top':
        rois, _ = self._proposal_top_layer(rpn_cls_prob, rpn_bbox_pred)
      else:
        raise NotImplementedError

    self._predictions["rpn_cls_score"] = rpn_cls_score
    self._predictions["rpn_cls_score_reshape"] = rpn_cls_score_reshape
    self._predictions["rpn_cls_prob"] = rpn_cls_prob
    self._predictions["rpn_cls_pred"] = rpn_cls_pred
    self._predictions["rpn_bbox_pred"] = rpn_bbox_pred
    self._predictions["rois"] = rois

    return rois

  def _region_classification(self, fc7):
    cls_score = self.cls_score_net(fc7)
    cls_pred = torch.max(cls_score, 1)[1]
    cls_prob = F.softmax(cls_score, dim=1)
    bbox_pred = self.bbox_pred_net(fc7)

    self._predictions["cls_score"] = cls_score
    self._predictions["cls_pred"] = cls_pred
    self._predictions["cls_prob"] = cls_prob
    self._predictions["bbox_pred"] = bbox_pred

    return cls_prob, bbox_pred

  def _image_to_head(self):
    raise NotImplementedError

  def _head_to_tail(self, pool5):
    raise NotImplementedError

  def create_architecture(self, num_classes, tag=None,
                          anchor_scales=(8, 16, 32), anchor_ratios=(0.5, 1, 2)):
    self._tag = tag

    self._num_classes = num_classes
    self._anchor_scales = anchor_scales
    self._num_scales = len(anchor_scales)

    self._anchor_ratios = anchor_ratios
    self._num_ratios = len(anchor_ratios)

    self._num_anchors = self._num_scales * self._num_ratios

    assert tag != None

    # Initialize layers
    self._init_modules()

  def _init_modules(self):
    self._init_head_tail()

    # rpn
    self.rpn_net = nn.Conv2d(self._net_conv_channels, cfg.RPN_CHANNELS, [3, 3], padding=1)

    self.rpn_cls_score_net = nn.Conv2d(cfg.RPN_CHANNELS, self._num_anchors * 2, [1, 1])
    
    self.rpn_bbox_pred_net = nn.Conv2d(cfg.RPN_CHANNELS, self._num_anchors * 4, [1, 1])

    self.cls_score_net = nn.Linear(self._fc7_channels, self._num_classes)
    self.bbox_pred_net = nn.Linear(self._fc7_channels, self._num_classes * 4)

    #discriminator for instance and image level
    self.D_inst = FCDiscriminator_inst(4096)
    self.D_img = FCDiscriminator_img(self._net_conv_channels)
    # self.D_img2 = FCDiscriminator_img(self._net_conv_channels)
    # self.D_img_domain = FCDiscriminator_img(self._net_conv_channels)

    # self.decoder = decoder(self._net_conv_channels)

    self.init_weights()

  def _run_summary_op(self, val=False):
    """
    Run the summary operator: feed the placeholders with corresponding newtork outputs(activations)
    """
    summaries = []
    # Add image gt
    summaries.append(self._add_gt_image_summary())
    # Add event_summaries
    for key, var in self._event_summaries.items():
      summaries.append(tb.summary.scalar(key, var.data[0]))
    self._event_summaries = {}
    if not val:
      # Add score summaries
      for key, var in self._score_summaries.items():
        summaries.append(self._add_score_summary(key, var))
      self._score_summaries = {}
      # Add act summaries
      for key, var in self._act_summaries.items():
        summaries += self._add_act_summary(key, var)
      self._act_summaries = {}
      # Add train summaries
      for k, var in dict(self.named_parameters()).items():
        if var.requires_grad:
          summaries.append(self._add_train_summary(k, var))

      self._image_gt_summaries = {}
    
    return summaries

  def _predict(self):
    # This is just _build_network in tf-faster-rcnn
    torch.backends.cudnn.benchmark = False
    net_conv = self._image_to_head()

    ##
    #net_conv2 = self._image_to_head_branch()
    #self.domain_feat = net_conv2

    # build the anchors for the image
    self._anchor_component(net_conv.size(2), net_conv.size(3))
   
    rois = self._region_proposal(net_conv)#error
    if cfg.POOLING_MODE == 'crop':
      pool5 = self._crop_pool_layer(net_conv, rois)
    else:
      pool5 = self._roi_pool_layer(net_conv, rois)

    if self._mode == 'TRAIN':
      torch.backends.cudnn.benchmark = True # benchmark because now the input size are fixed
    fc7 = self._head_to_tail(pool5)

    cls_prob, bbox_pred = self._region_classification(fc7)
    
    for k in self._predictions.keys():
      self._score_summaries[k] = self._predictions[k]

    return rois, cls_prob, bbox_pred, net_conv, fc7
  
  def _clip_boxes(self, boxes, im_shape):
    """Clip boxes to image boundaries."""
    # x1 >= 0
    boxes[:, 0::4] = np.maximum(boxes[:, 0::4], 0)
    # y1 >= 0
    boxes[:, 1::4] = np.maximum(boxes[:, 1::4], 0)
    # x2 < im_shape[1]
    boxes[:, 2::4] = np.minimum(boxes[:, 2::4], im_shape[1] - 1)
    # y2 < im_shape[0]
    boxes[:, 3::4] = np.minimum(boxes[:, 3::4], im_shape[0] - 1)
    return boxes
  
  def forward(self, image, im_info, gt_boxes=None, mode='TRAIN', adapt=None):
    self._image_gt_summaries['image'] = image
    self._image_gt_summaries['gt_boxes'] = gt_boxes
    self._image_gt_summaries['im_info'] = im_info

    self._image = Variable(torch.from_numpy(image.transpose([0,3,1,2])).cuda(), volatile=mode == 'TEST')
    self._im_info = im_info # No need to change; actually it can be an list
    self._gt_boxes = Variable(torch.from_numpy(gt_boxes).cuda()) if gt_boxes is not None else None

    self._mode = mode

    rois, cls_prob, bbox_pred, net_conv, fc7 = self._predict()

    if mode == 'TEST':
      stds = bbox_pred.data.new(cfg.TRAIN.BBOX_NORMALIZE_STDS).repeat(self._num_classes).unsqueeze(0).expand_as(bbox_pred)
      means = bbox_pred.data.new(cfg.TRAIN.BBOX_NORMALIZE_MEANS).repeat(self._num_classes).unsqueeze(0).expand_as(bbox_pred)
      self._predictions["bbox_pred"] = bbox_pred.mul(Variable(stds)).add(Variable(means))
    elif adapt:
      pass
    else:
      self._add_losses() # compute losses

    # if not adapt and mode != 'TEST':
    #   scores = np.reshape(cls_prob.data.cpu().numpy(), [cls_prob.shape[0], -1])
    #   bbox_pred = np.reshape(bbox_pred.data.cpu().numpy(), [bbox_pred.shape[0], -1])
    #   boxes = self._predictions['rois'].data.cpu().numpy()[:, 1:5] / im_info[2]

    #   if cfg.TEST.BBOX_REG:
    #     # Apply bounding-box regression deltas
    #     box_deltas = bbox_pred
    #     pred_boxes = bbox_transform_inv(torch.from_numpy(boxes), torch.from_numpy(box_deltas)).numpy()
    #     pred_boxes = self._clip_boxes(pred_boxes, im_info[3:])
    #   else:
    #     # Simply repeat the boxes, once for each class
    #     pred_boxes = np.tile(boxes, (1, scores.shape[1]))
    #   inds = []
    #   if len(gt_boxes) > 0:
    #     j = 1 #class 'car'
    #     for idx, bb in enumerate(pred_boxes[:,j*4:(j+1)*4]):
    #       ixmin = np.maximum(gt_boxes[:, 0], bb[0])
    #       iymin = np.maximum(gt_boxes[:, 1], bb[1])
    #       ixmax = np.minimum(gt_boxes[:, 2], bb[2])
    #       iymax = np.minimum(gt_boxes[:, 3], bb[3])
    #       iw = np.maximum(ixmax - ixmin + 1., 0.)
    #       ih = np.maximum(iymax - iymin + 1., 0.)
    #       inters = iw * ih

    #       # union
    #       uni = ((bb[2] - bb[0] + 1.) * (bb[3] - bb[1] + 1.) +
    #              (gt_boxes[:, 2] - gt_boxes[:, 0] + 1.) *
    #              (gt_boxes[:, 3] - gt_boxes[:, 1] + 1.) - inters)

    #       overlaps = inters / uni
    #       ovmax = np.max(overlaps)
    #       jmax = np.argmax(overlaps)
          
    #       if ovmax < 0.5:
    #         inds.append(idx)

    #   return fc7, net_conv, np.asarray(list(set(inds)))

    # elif mode != "TEST":
    #   scores = np.reshape(cls_prob.data.cpu().numpy(), [cls_prob.shape[0], -1])
    #   #keep car prediction
    #   inds = np.where(scores[:,1] < 0.5)[0]
    #   # print(inds.shape, 'T')
    #   return fc7, net_conv, inds

    return fc7, net_conv

  def init_weights(self):
    def normal_init(m, mean, stddev, truncated=False):
      """
      weight initalizer: truncated normal and random normal.
      """
      # x is a parameter
      if truncated:
        m.weight.data.normal_().fmod_(2).mul_(stddev).add_(mean) # not a perfect approximation
      else:
        m.weight.data.normal_(mean, stddev)
      m.bias.data.zero_()
      
    normal_init(self.rpn_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
    normal_init(self.rpn_cls_score_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
    normal_init(self.rpn_bbox_pred_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
    normal_init(self.cls_score_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
    normal_init(self.bbox_pred_net, 0, 0.001, cfg.TRAIN.TRUNCATED)

    normal_init(self.D_inst.fc1, 0, 0.01, cfg.TRAIN.TRUNCATED)
    normal_init(self.D_inst.fc2, 0, 0.01, cfg.TRAIN.TRUNCATED)
    normal_init(self.D_inst.fc3, 0, 0.01, cfg.TRAIN.TRUNCATED)
    normal_init(self.D_inst.classifier, 0, 0.01, cfg.TRAIN.TRUNCATED)

    normal_init(self.D_img.conv1, 0, 0.01, cfg.TRAIN.TRUNCATED)
    normal_init(self.D_img.conv2, 0, 0.01, cfg.TRAIN.TRUNCATED)
    normal_init(self.D_img.conv3, 0, 0.01, cfg.TRAIN.TRUNCATED)
    normal_init(self.D_img.classifier, 0, 0.01, cfg.TRAIN.TRUNCATED)

    # normal_init(self.D_img2.conv1, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.D_img2.conv2, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.D_img2.conv3, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.D_img2.classifier, 0, 0.01, cfg.TRAIN.TRUNCATED)

    # normal_init(self.D_img_domain.conv1, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.D_img_domain.conv2, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.D_img_domain.conv3, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.D_img_domain.classifier, 0, 0.01, cfg.TRAIN.TRUNCATED)

  # Extract the head feature maps, for example for vgg16 it is conv5_3
  # only useful during testing mode
  def extract_head(self, image):
    feat = self._layers["head"](Variable(torch.from_numpy(image.transpose([0,3,1,2])).cuda(), volatile=True))
    return feat

  # only useful during testing mode
  def test_image(self, image, im_info):
    self.eval()
    fc7, net_conv = self.forward(image, im_info, None, mode='TEST')
    cls_score, cls_prob, bbox_pred, rois = self._predictions["cls_score"].data.cpu().numpy(), \
                                                     self._predictions['cls_prob'].data.cpu().numpy(), \
                                                     self._predictions['bbox_pred'].data.cpu().numpy(), \
                                                     self._predictions['rois'].data.cpu().numpy()
    # im = self.decoder(net_conv)
    # im = (im[0].cpu().data.numpy() * 255).astype(np.uint8).transpose([1,2,0])
    # cv2.imwrite('./test.png', im)

    return cls_score, cls_prob, bbox_pred, rois, fc7, net_conv

  def delete_intermediate_states(self):
    # Delete intermediate result to save memory
    for d in [self._losses, self._predictions, self._anchor_targets, self._proposal_targets]:
      for k in list(d):
        del d[k]

  def get_summary(self, blobs):
    self.eval()
    self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'])
    self.train()
    summary = self._run_summary_op(True)

    return summary

  def train_adapt_step_branch(self, blobs_S, blobs_T, train_op, D_inst_op, D_img_op, D_img_branch_op):
    source_label = 0
    target_label = 1

    train_op.zero_grad()
    # D_inst_op.zero_grad()
    D_img_op.zero_grad()
    D_img_branch_op.zero_grad()
    
    # sig = nn.Sigmoid()
    bceLoss_func = nn.BCEWithLogitsLoss()
    diffLoss = DiffLoss()
    # interp_S = nn.Upsample(size=(blobs_S['data'].shape[1], blobs_S['data'].shape[2]), mode='bilinear')
    # interp_T = nn.Upsample(size=(blobs_T['data'].shape[1], blobs_T['data'].shape[2]), mode='bilinear')

    #train with source
    fc7, net_conv = self.forward(blobs_S['data'], blobs_S['im_info'], blobs_S['gt_boxes'])
    # fc7 = fc7.detach()
    # net_conv = net_conv.detach()
    # fc7 = grad_reverse(fc7)
    
    ##diff loss with domain feat
    loss_diff_S = diffLoss(net_conv, self.domain_feat)

    net_conv = grad_reverse(net_conv)
    # net_conv = interp_S(net_conv)
    #det loss
    loss_S = self._losses['total_loss']
    #D_inst
    # D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img(net_conv)

    #loss
    # loss_D_inst_S = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(source_label)).cuda()) 
    loss_D_img_S = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(source_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_S =  Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_S += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_S /= sig_D_inst_out.size()[0]

    D_img_domain_out = self.D_img_domain(self.domain_feat)
    loss_D_img_domain_S = bceLoss_func(D_img_domain_out, Variable(torch.FloatTensor(D_img_domain_out.data.size()).fill_(source_label)).cuda())
    
    total_loss_S = loss_S + (cfg.ADAPT_LAMBDA/2.) * (loss_D_img_S + loss_diff_S + loss_D_img_domain_S)#(loss_D_inst_S + loss_D_img_S + loss_D_const_S)
    #total_loss_S.backward()

    # print("S")
    # print(loss_S.data[0], loss_D_inst_S.data[0], loss_D_img_S.data[0], loss_D_const_S.data[0], mean.data[0], source_label)

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]
    #train with target
    fc7, net_conv = self.forward(blobs_T['data'], blobs_T['im_info'], blobs_T['gt_boxes'], adapt=True)
    ##diff loss with domain feat
    loss_diff_T = diffLoss(net_conv, self.domain_feat)
    # fc7 = fc7.detach()
    # net_conv = net_conv.detach()
    # self.vgg.features[28].register_backward_hook(printgradnorm)
    # fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)
    # net_conv = interp_T(net_conv)

    #D_inst
    # D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img(net_conv)
    #self.D_img.conv3.register_backward_hook(printgradnorm)
    #loss
    # loss_D_inst_T = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(target_label)).cuda()) 
    loss_D_img_T = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(target_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_T = Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_T += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_T /= sig_D_inst_out.size()[0]

    D_img_domain_out = self.D_img_domain(self.domain_feat)
    loss_D_img_domain_T = bceLoss_func(D_img_domain_out, Variable(torch.FloatTensor(D_img_domain_out.data.size()).fill_(target_label)).cuda())

    total_loss_T = (cfg.ADAPT_LAMBDA/2.) * (loss_D_img_T + loss_diff_T + loss_D_img_domain_T)#(loss_D_inst_T + loss_D_img_T + loss_D_const_T)
    #total_loss_T.backward()

    total_loss = total_loss_S + total_loss_T
    total_loss.backward()
    
    #clip gradient
    # clip = 100
    # torch.nn.utils.clip_grad_norm(self.D_inst.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.D_img.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.parameters(),clip)

    # print("T")
    # print(loss_D_inst_T.data[0], loss_D_img_T.data[0], loss_D_const_T.data[0], mean.data[0], target_label)


    train_op.step()
    # D_inst_op.step()
    D_img_op.step()
    D_img_branch_op.step()
                                                                        
    self.delete_intermediate_states()

    loss_D_inst_S, loss_D_const_S, loss_D_inst_T, loss_D_const_T = 0, 0, 0, 0
    # loss_D_img_S, loss_D_const_S, loss_D_img_T, loss_D_const_T = 0, 0, 0, 0

    # loss_D_const_S, loss_D_const_T = 0, 0
    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, loss_D_inst_S, loss_D_img_S, loss_D_const_S, loss_D_inst_T, loss_D_img_T, loss_D_const_T, loss_diff_S, loss_diff_T, loss_D_img_domain_S, loss_D_img_domain_T

  def train_focus_inst_adapt_step(self, blobs_S, blobs_T, train_op, D_inst_op, D_img_op):
    source_label = 0
    target_label = 1

    train_op.zero_grad()
    D_inst_op.zero_grad()
    
    bceLoss_func = nn.BCEWithLogitsLoss()

    #train with source
    fc7, net_conv, keep_inds = self.forward(blobs_S['data'], blobs_S['im_info'], blobs_S['gt_boxes'])
    loss_S = self._losses['total_loss']

    if len(keep_inds) == 0:
      total_loss_S = loss_S
      loss_D_inst_S = 0
      print('skipS')
    else:
      fc7 = grad_reverse(fc7)

      #D_inst
      D_inst_out = self.D_inst(fc7)[keep_inds]

      #loss
      loss_D_inst_S = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(source_label)).cuda()) 
      
      total_loss_S = loss_S + (cfg.ADAPT_LAMBDA/2.) * loss_D_inst_S
      #total_loss_S.backward()

      # print("S")
      # print(loss_S.data[0], loss_D_inst_S.data[0], loss_D_img_S.data[0], loss_D_const_S.data[0], mean.data[0], source_label)

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]
    #train with target
    fc7, net_conv, keep_inds = self.forward(blobs_T['data'], blobs_T['im_info'], blobs_T['gt_boxes'], adapt=True)
    if len(keep_inds) == 0:
      total_loss_T = 0
      loss_D_inst_T = 0
      print("skipT")
    else:
      fc7 = grad_reverse(fc7)

      #D_inst
      D_inst_out = self.D_inst(fc7)[keep_inds]

      #loss
      loss_D_inst_T = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(target_label)).cuda()) 

      total_loss_T = (cfg.ADAPT_LAMBDA/2.) * loss_D_inst_T
      #total_loss_T.backward()

    total_loss = total_loss_S + total_loss_T
    total_loss.backward()
    
    #clip gradient
    # clip = 5
    # torch.nn.utils.clip_grad_norm(self.D_inst.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.D_img.parameters(),clip)

    # print("T")
    # print(loss_D_inst_T.data[0], loss_D_img_T.data[0], loss_D_const_T.data[0], mean.data[0], target_label)


    train_op.step()
    D_inst_op.step()
                                                                        
    self.delete_intermediate_states()
    print("============")
    loss_D_img_S, loss_D_const_S, loss_D_img_T, loss_D_const_T = 0, 0, 0, 0

    # loss_D_const_S, loss_D_const_T = 0, 0
    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, loss_D_inst_S, loss_D_img_S, loss_D_const_S, loss_D_inst_T, loss_D_img_T, loss_D_const_T

  def train_adapt_step_SST(self, blobs_S, blobs_T, blobs_synth, train_op, D_inst_op, D_img_op, D_img2_op):
    source_label = 0
    target_label = 1

    train_op.zero_grad()
    # D_inst_op.zero_grad()
    D_img_op.zero_grad()
    D_img2_op.zero_grad()
    
    # sig = nn.Sigmoid()
    bceLoss_func = nn.BCEWithLogitsLoss()
    # interp_S = nn.Upsample(size=(blobs_S['data'].shape[1], blobs_S['data'].shape[2]), mode='bilinear')
    # interp_T = nn.Upsample(size=(blobs_T['data'].shape[1], blobs_T['data'].shape[2]), mode='bilinear')

    #train with source
    fc7, net_conv = self.forward(blobs_S['data'], blobs_S['im_info'], blobs_S['gt_boxes'])
    # fc7 = fc7.detach()
    # net_conv = net_conv.detach()
    # fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)

    # net_conv = interp_S(net_conv)
    #det loss
    loss_S = self._losses['total_loss']
    #D_inst
    # D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img(net_conv)

    #loss
    # loss_D_inst_S = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(source_label)).cuda()) 
    loss_D_img_S = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(source_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_S =  Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_S += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_S /= sig_D_inst_out.size()[0]
    
    total_loss_S = loss_S + (cfg.ADAPT_LAMBDA/2.) * loss_D_img_S#(loss_D_inst_S + loss_D_img_S + loss_D_const_S)
    # total_loss_S.backward()

    # print("S")
    # print(loss_S.data[0], loss_D_inst_S.data[0], loss_D_img_S.data[0], loss_D_const_S.data[0], mean.data[0], source_label)

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]

    #train with synthetic source
    fc7, net_conv = self.forward(blobs_synth['data'], blobs_synth['im_info'], blobs_synth['gt_boxes'])
    # fc7 = fc7.detach()
    # net_conv = net_conv.detach()
    # fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)

    # net_conv = interp_S(net_conv)
    #det loss
    loss_synth = self._losses['total_loss']
    #D_inst
    # D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img(net_conv)

    D_img2_out = self.D_img2(net_conv)

    #loss
    # loss_D_inst_S = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(source_label)).cuda()) 
    loss_D_img_synth = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(target_label)).cuda())

    loss_D_img2_synth = bceLoss_func(D_img2_out, Variable(torch.FloatTensor(D_img2_out.data.size()).fill_(source_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_S =  Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_S += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_S /= sig_D_inst_out.size()[0]
    
    total_loss_synth = loss_synth + (cfg.ADAPT_LAMBDA/2.) * loss_D_img_synth + (cfg.ADAPT_LAMBDA/2.) * loss_D_img2_synth#(loss_D_inst_S + loss_D_img_S + loss_D_const_S)
    # total_loss_S.backward()

    # print("S")
    # print(loss_S.data[0], loss_D_inst_S.data[0], loss_D_img_S.data[0], loss_D_const_S.data[0], mean.data[0], source_label)

    rpn_loss_cls_synth, rpn_loss_box_synth, loss_cls_synth, loss_box_synth, loss_synth = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]

    #train with target
    fc7, net_conv = self.forward(blobs_T['data'], blobs_T['im_info'], blobs_T['gt_boxes'], adapt=True)
    # fc7 = fc7.detach()
    # net_conv = net_conv.detach()
    # self.vgg.features[28].register_backward_hook(printgradnorm)
    # fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)

    # net_conv = interp_T(net_conv)

    #D_inst
    # D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img2(net_conv)
    #self.D_img.conv3.register_backward_hook(printgradnorm)
    #loss
    # loss_D_inst_T = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(target_label)).cuda()) 
    loss_D_img_T = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(target_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_T = Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_T += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_T /= sig_D_inst_out.size()[0]

    total_loss_T = (cfg.ADAPT_LAMBDA/2.) * loss_D_img_T#(loss_D_inst_T + loss_D_img_T + loss_D_const_T)
    #total_loss_T.backward()

    total_loss = total_loss_S + total_loss_T + total_loss_synth
    total_loss.backward()
    
    #clip gradient
    # clip = 100
    # torch.nn.utils.clip_grad_norm(self.D_inst.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.D_img.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.parameters(),clip)

    # print("T")
    # print(loss_D_inst_T.data[0], loss_D_img_T.data[0], loss_D_const_T.data[0], mean.data[0], target_label)


    train_op.step()
    # D_inst_op.step()
    D_img_op.step()
    D_img2_op.step()
                                                                        
    self.delete_intermediate_states()

    loss_D_inst_S, loss_D_const_S, loss_D_inst_T, loss_D_const_T = 0, 0, 0, 0
    # loss_D_img_S, loss_D_const_S, loss_D_img_T, loss_D_const_T = 0, 0, 0, 0

    # loss_D_const_S, loss_D_const_T = 0, 0
    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, loss_D_inst_S, loss_D_img_S, loss_D_const_S, loss_D_inst_T, loss_D_img_T, loss_D_const_T, \
           rpn_loss_cls_synth, rpn_loss_box_synth, loss_cls_synth, loss_box_synth, loss_synth, loss_D_img_synth, loss_D_img2_synth
           
  def train_adapt_step_img_inst(self, blobs_S, blobs_T, train_op, D_inst_op, D_img_op):
    source_label = 0
    target_label = 1

    train_op.zero_grad()
    D_inst_op.zero_grad()
    D_img_op.zero_grad()
    
    # sig = nn.Sigmoid()
    bceLoss_func = nn.BCEWithLogitsLoss()
    # interp_S = nn.Upsample(size=(blobs_S['data'].shape[1], blobs_S['data'].shape[2]), mode='bilinear')
    # interp_T = nn.Upsample(size=(blobs_T['data'].shape[1], blobs_T['data'].shape[2]), mode='bilinear')

    #train with source
    fc7, net_conv = self.forward(blobs_S['data'], blobs_S['im_info'], blobs_S['gt_boxes'])
    # fc7 = fc7.detach()
    # net_conv = net_conv.detach()
    fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)

    # net_conv = interp_S(net_conv)
    #det loss
    loss_S = self._losses['total_loss']
    #D_inst
    D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img(net_conv)

    #loss
    loss_D_inst_S = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(source_label)).cuda()) 
    loss_D_img_S = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(source_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_S =  Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_S += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_S /= sig_D_inst_out.size()[0]
    
    total_loss_S = loss_S + (cfg.ADAPT_LAMBDA/2.) * (loss_D_inst_S + loss_D_img_S)#(loss_D_inst_S + loss_D_img_S + loss_D_const_S)
    # total_loss_S.backward()

    # print("S")
    # print(loss_S.data[0], loss_D_inst_S.data[0], loss_D_img_S.data[0], loss_D_const_S.data[0], mean.data[0], source_label)

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]
    #train with target
    fc7, net_conv = self.forward(blobs_T['data'], blobs_T['im_info'], blobs_T['gt_boxes'], adapt=True)
    # fc7 = fc7.detach()
    # net_conv = net_conv.detach()
    # self.vgg.features[28].register_backward_hook(printgradnorm)
    fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)

    # net_conv = interp_T(net_conv)

    #D_inst
    D_inst_out = self.D_inst(fc7)
    # D_img
    D_img_out = self.D_img(net_conv)
    #self.D_img.conv3.register_backward_hook(printgradnorm)
    #loss
    loss_D_inst_T = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(target_label)).cuda()) 
    loss_D_img_T = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(target_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_T = Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_T += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_T /= sig_D_inst_out.size()[0]

    total_loss_T = (cfg.ADAPT_LAMBDA/2.) * (loss_D_inst_T + loss_D_img_T)#(loss_D_inst_T + loss_D_img_T + loss_D_const_T)
    #total_loss_T.backward()

    total_loss = total_loss_S + total_loss_T
    total_loss.backward()
    
    #clip gradient
    # clip = 100
    # torch.nn.utils.clip_grad_norm(self.D_inst.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.D_img.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.parameters(),clip)

    # print("T")
    # print(loss_D_inst_T.data[0], loss_D_img_T.data[0], loss_D_const_T.data[0], mean.data[0], target_label)


    train_op.step()
    D_inst_op.step()
    D_img_op.step()
                                                                        
    self.delete_intermediate_states()

    # loss_D_inst_S, loss_D_const_S, loss_D_inst_T, loss_D_const_T = 0, 0, 0, 0
    # loss_D_img_S, loss_D_const_S, loss_D_img_T, loss_D_const_T = 0, 0, 0, 0

    loss_D_const_S, loss_D_const_T = 0, 0
    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, loss_D_inst_S, loss_D_img_S, loss_D_const_S, loss_D_inst_T, loss_D_img_T, loss_D_const_T

  def train_adapt_step_img_inst_const(self, blobs_S, blobs_T, train_op, D_inst_op, D_img_op):
    source_label = 0
    target_label = 1

    train_op.zero_grad()
    D_inst_op.zero_grad()
    D_img_op.zero_grad()
    
    sig = nn.Sigmoid()
    bceLoss_func = nn.BCEWithLogitsLoss()

    #train with source
    fc7, net_conv = self.forward(blobs_S['data'], blobs_S['im_info'], blobs_S['gt_boxes'])
    fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)

    #det loss
    loss_S = self._losses['total_loss']
    #D_inst
    D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img(net_conv)

    #loss
    loss_D_inst_S = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(source_label)).cuda()) 
    loss_D_img_S = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(source_label)).cuda())

    sig_D_inst_out = sig(D_inst_out)
    sig_D_img_out = sig(D_img_out)
    mean = sig_D_img_out.mean()

    loss_D_const_S =  Variable(torch.FloatTensor(1).zero_()).cuda()
    for j in range(sig_D_inst_out.size()[0]):
      loss_D_const_S += torch.dist(mean, sig_D_inst_out[j][0])
    loss_D_const_S /= sig_D_inst_out.size()[0]
    
    total_loss_S = loss_S + (cfg.ADAPT_LAMBDA/2.) * (loss_D_inst_S + loss_D_img_S + loss_D_const_S)
    # total_loss_S.backward()

    # print("S")
    # print(loss_S.data[0], loss_D_inst_S.data[0], loss_D_img_S.data[0], loss_D_const_S.data[0], mean.data[0], source_label)

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]
    #train with target
    fc7, net_conv = self.forward(blobs_T['data'], blobs_T['im_info'], blobs_T['gt_boxes'], adapt=True)
    fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)

    #D_inst
    D_inst_out = self.D_inst(fc7)
    # D_img
    D_img_out = self.D_img(net_conv)
    #loss
    loss_D_inst_T = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(target_label)).cuda()) 
    loss_D_img_T = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(target_label)).cuda())

    sig_D_inst_out = sig(D_inst_out)
    sig_D_img_out = sig(D_img_out)
    mean = sig_D_img_out.mean()

    loss_D_const_T = Variable(torch.FloatTensor(1).zero_()).cuda()
    for j in range(sig_D_inst_out.size()[0]):
      loss_D_const_T += torch.dist(mean, sig_D_inst_out[j][0])
    loss_D_const_T /= sig_D_inst_out.size()[0]

    total_loss_T = (cfg.ADAPT_LAMBDA/2.) * (loss_D_inst_T + loss_D_img_T + loss_D_const_T)
    #total_loss_T.backward()

    total_loss = total_loss_S + total_loss_T
    total_loss.backward()
    
    #clip gradient
    # clip = 100
    # torch.nn.utils.clip_grad_norm(self.D_inst.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.D_img.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.parameters(),clip)

    # print("T")
    # print(loss_D_inst_T.data[0], loss_D_img_T.data[0], loss_D_const_T.data[0], mean.data[0], target_label)

    train_op.step()
    D_inst_op.step()
    D_img_op.step()
                                                                        
    self.delete_intermediate_states()

    # loss_D_inst_S, loss_D_const_S, loss_D_inst_T, loss_D_const_T = 0, 0, 0, 0
    # loss_D_img_S, loss_D_const_S, loss_D_img_T, loss_D_const_T = 0, 0, 0, 0

    # loss_D_const_S, loss_D_const_T = 0, 0
    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, loss_D_inst_S, loss_D_img_S, loss_D_const_S, loss_D_inst_T, loss_D_img_T, loss_D_const_T

  def train_adapt_step_img_inst(self, blobs_S, blobs_T, train_op, D_inst_op, D_img_op):
    source_label = 0
    target_label = 1

    train_op.zero_grad()
    D_inst_op.zero_grad()
    D_img_op.zero_grad()
    
    # sig = nn.Sigmoid()
    bceLoss_func = nn.BCEWithLogitsLoss()
    # interp_S = nn.Upsample(size=(blobs_S['data'].shape[1], blobs_S['data'].shape[2]), mode='bilinear')
    # interp_T = nn.Upsample(size=(blobs_T['data'].shape[1], blobs_T['data'].shape[2]), mode='bilinear')

    #train with source
    fc7, net_conv = self.forward(blobs_S['data'], blobs_S['im_info'], blobs_S['gt_boxes'])
    # fc7 = fc7.detach()
    # net_conv = net_conv.detach()
    fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)

    # net_conv = interp_S(net_conv)
    #det loss
    loss_S = self._losses['total_loss']
    #D_inst
    D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img(net_conv)

    #loss
    loss_D_inst_S = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(source_label)).cuda()) 
    loss_D_img_S = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(source_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_S =  Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_S += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_S /= sig_D_inst_out.size()[0]
    
    total_loss_S = loss_S + (cfg.ADAPT_LAMBDA/2.) * (loss_D_inst_S + loss_D_img_S)#(loss_D_inst_S + loss_D_img_S + loss_D_const_S)
    # total_loss_S.backward()

    # print("S")
    # print(loss_S.data[0], loss_D_inst_S.data[0], loss_D_img_S.data[0], loss_D_const_S.data[0], mean.data[0], source_label)

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]
    #train with target
    fc7, net_conv = self.forward(blobs_T['data'], blobs_T['im_info'], blobs_T['gt_boxes'], adapt=True)
    # fc7 = fc7.detach()
    # net_conv = net_conv.detach()
    # self.vgg.features[28].register_backward_hook(printgradnorm)
    fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)

    # net_conv = interp_T(net_conv)

    #D_inst
    D_inst_out = self.D_inst(fc7)
    # D_img
    D_img_out = self.D_img(net_conv)
    #self.D_img.conv3.register_backward_hook(printgradnorm)
    #loss
    loss_D_inst_T = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(target_label)).cuda()) 
    loss_D_img_T = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(target_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_T = Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_T += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_T /= sig_D_inst_out.size()[0]

    total_loss_T = (cfg.ADAPT_LAMBDA/2.) * (loss_D_inst_T + loss_D_img_T)#(loss_D_inst_T + loss_D_img_T + loss_D_const_T)
    #total_loss_T.backward()

    total_loss = total_loss_S + total_loss_T
    total_loss.backward()
    
    #clip gradient
    # clip = 100
    # torch.nn.utils.clip_grad_norm(self.D_inst.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.D_img.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.parameters(),clip)

    # print("T")
    # print(loss_D_inst_T.data[0], loss_D_img_T.data[0], loss_D_const_T.data[0], mean.data[0], target_label)


    train_op.step()
    D_inst_op.step()
    D_img_op.step()
                                                                        
    self.delete_intermediate_states()

    # loss_D_inst_S, loss_D_const_S, loss_D_inst_T, loss_D_const_T = 0, 0, 0, 0
    # loss_D_img_S, loss_D_const_S, loss_D_img_T, loss_D_const_T = 0, 0, 0, 0

    loss_D_const_S, loss_D_const_T = 0, 0
    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, loss_D_inst_S, loss_D_img_S, loss_D_const_S, loss_D_inst_T, loss_D_img_T, loss_D_const_T

  def train_adapt_step_inst(self, blobs_S, blobs_T, train_op, D_inst_op, D_img_op):
    source_label = 0
    target_label = 1

    train_op.zero_grad()
    D_inst_op.zero_grad()
    
    bceLoss_func = nn.BCEWithLogitsLoss()

    #train with source
    fc7, net_conv = self.forward(blobs_S['data'], blobs_S['im_info'], blobs_S['gt_boxes'])

    fc7 = grad_reverse(fc7)

    #det loss
    loss_S = self._losses['total_loss']
    #D_inst
    D_inst_out = self.D_inst(fc7)

    #loss
    loss_D_inst_S = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(source_label)).cuda()) 
    
    total_loss_S = loss_S + (cfg.ADAPT_LAMBDA/2.) * loss_D_inst_S
    # total_loss_S.backward()

    # print("S")
    # print(loss_S.data[0], loss_D_inst_S.data[0], loss_D_img_S.data[0], loss_D_const_S.data[0], mean.data[0], source_label)

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]
    #train with target
    fc7, net_conv = self.forward(blobs_T['data'], blobs_T['im_info'], blobs_T['gt_boxes'], adapt=True)

    fc7 = grad_reverse(fc7)

    #D_inst
    D_inst_out = self.D_inst(fc7)

    #loss
    loss_D_inst_T = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(target_label)).cuda()) 

    total_loss_T = (cfg.ADAPT_LAMBDA/2.) * loss_D_inst_T
    #total_loss_T.backward()

    total_loss = total_loss_S + total_loss_T
    total_loss.backward()
    
    #clip gradient
    # clip = 100
    # torch.nn.utils.clip_grad_norm(self.D_inst.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.D_img.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.parameters(),clip)

    # print("T")
    # print(loss_D_inst_T.data[0], loss_D_img_T.data[0], loss_D_const_T.data[0], mean.data[0], target_label)


    train_op.step()
    D_inst_op.step()
                                                                        
    self.delete_intermediate_states()

    loss_D_img_S, loss_D_const_S, loss_D_img_T, loss_D_const_T = 0, 0, 0, 0

    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, loss_D_inst_S, loss_D_img_S, loss_D_const_S, loss_D_inst_T, loss_D_img_T, loss_D_const_T


  def train_adapt_step_img(self, blobs_S, blobs_T, train_op, D_inst_op, D_img_op):
    source_label = 0
    target_label = 1

    train_op.zero_grad()
    # D_inst_op.zero_grad()
    D_img_op.zero_grad()
    
    # sig = nn.Sigmoid()
    bceLoss_func = nn.BCEWithLogitsLoss()
    # interp_S = nn.Upsample(size=(blobs_S['data'].shape[1], blobs_S['data'].shape[2]), mode='bilinear')
    # interp_T = nn.Upsample(size=(blobs_T['data'].shape[1], blobs_T['data'].shape[2]), mode='bilinear')

    #train with source
    fc7, net_conv = self.forward(blobs_S['data'], blobs_S['im_info'], blobs_S['gt_boxes'])
    # fc7 = fc7.detach()
    # net_conv = net_conv.detach()
    # fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)

    # net_conv = interp_S(net_conv)
    #det loss
    loss_S = self._losses['total_loss']
    #D_inst
    # D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img(net_conv)

    #loss
    # loss_D_inst_S = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(source_label)).cuda()) 
    loss_D_img_S = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(source_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_S =  Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_S += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_S /= sig_D_inst_out.size()[0]
    
    total_loss_S = loss_S + (cfg.ADAPT_LAMBDA/2.) * loss_D_img_S#(loss_D_inst_S + loss_D_img_S + loss_D_const_S)
    # total_loss_S.backward()

    # print("S")
    # print(loss_S.data[0], loss_D_inst_S.data[0], loss_D_img_S.data[0], loss_D_const_S.data[0], mean.data[0], source_label)

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]
    #train with target
    fc7, net_conv = self.forward(blobs_T['data'], blobs_T['im_info'], blobs_T['gt_boxes'], adapt=True)
    # fc7 = fc7.detach()
    # net_conv = net_conv.detach()
    # self.vgg.features[28].register_backward_hook(printgradnorm)
    # fc7 = grad_reverse(fc7)
    net_conv = grad_reverse(net_conv)

    # net_conv = interp_T(net_conv)

    #D_inst
    # D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img(net_conv)
    #self.D_img.conv3.register_backward_hook(printgradnorm)
    #loss
    # loss_D_inst_T = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(target_label)).cuda()) 
    loss_D_img_T = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(target_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_T = Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_T += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_T /= sig_D_inst_out.size()[0]

    total_loss_T = (cfg.ADAPT_LAMBDA/2.) * loss_D_img_T#(loss_D_inst_T + loss_D_img_T + loss_D_const_T)
    #total_loss_T.backward()

    total_loss = total_loss_S + total_loss_T
    # total_loss = (total_loss_S + total_loss_T) / 2. # subIters
    total_loss.backward()
    
    #clip gradient
    # clip = 100
    # torch.nn.utils.clip_grad_norm(self.D_inst.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.D_img.parameters(),clip)
    # torch.nn.utils.clip_grad_norm(self.parameters(),clip)

    # print("T")
    # print(loss_D_inst_T.data[0], loss_D_img_T.data[0], loss_D_const_T.data[0], mean.data[0], target_label)


    train_op.step()
    # D_inst_op.step()
    D_img_op.step()
                                                                        
    self.delete_intermediate_states()

    loss_D_inst_S, loss_D_const_S, loss_D_inst_T, loss_D_const_T = 0, 0, 0, 0
    # loss_D_img_S, loss_D_const_S, loss_D_img_T, loss_D_const_T = 0, 0, 0, 0

    # loss_D_const_S, loss_D_const_T = 0, 0
    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, loss_D_inst_S, loss_D_img_S, loss_D_const_S, loss_D_inst_T, loss_D_img_T, loss_D_const_T

  def train_adapt_adversarial_step(self, blobs_S, blobs_T, train_op, D_inst_op, D_img_op):
    source_label = 0
    target_label = 1
    train_op.zero_grad()
    # D_inst_op.zero_grad()
    D_img_op.zero_grad()

    # sig = nn.Sigmoid()
    bceLoss_func = nn.BCEWithLogitsLoss()

    
    # for p in self.D_inst.parameters():
    #   p.require_grad = False
    for p in self.D_img.parameters():
      p.require_grad = False

    #train with source
    fc7_S, net_conv_S = self.forward(blobs_S['data'], blobs_S['im_info'], blobs_S['gt_boxes'])
    loss_det = self._losses['total_loss']
    loss_det.backward()

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                            self._losses['rpn_loss_box'].data[0], \
                                                            self._losses['cross_entropy'].data[0], \
                                                            self._losses['loss_box'].data[0], \
                                                            self._losses['total_loss'].data[0]

    #train with target
    fc7_T, net_conv_T = self.forward(blobs_T['data'], blobs_T['im_info'], blobs_T['gt_boxes'], adapt=True)
    # D_inst_out_T = self.D_inst(fc7_T)
    D_img_out_T = self.D_img(net_conv_T)

    # loss_D_inst_adv_T = bceLoss_func(D_inst_out_T, Variable(torch.FloatTensor(D_inst_out_T.data.size()).fill_(source_label)).cuda()) 
    loss_D_img_adv_T = bceLoss_func(D_img_out_T, Variable(torch.FloatTensor(D_img_out_T.data.size()).fill_(source_label)).cuda())

    # sig_D_inst_out_T = sig(D_inst_out_T)
    # sig_D_img_out_T = sig(D_img_out_T)
    # mean = sig_D_img_out_T.mean()

    # loss_D_const_adv_T = Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out_T.size()[0]):
    #   loss_D_const_adv_T += torch.dist(mean, sig_D_inst_out_T[j][0])
    # loss_D_const_adv_T /= sig_D_inst_out_T.size()[0]

    loss_adv = cfg.ADAPT_LAMBDA * loss_D_img_adv_T#(loss_D_inst_adv_T + loss_D_img_adv_T + loss_D_const_adv_T)
    loss_adv.backward()

    #train D_inst and D_img
    # for p in self.D_inst.parameters():
    #   p.require_grad = True
    for p in self.D_img.parameters():
      p.require_grad = True
    #Source
    # fc7_S = fc7_S.detach()
    net_conv_S = net_conv_S.detach()

    # D_inst_out_S = self.D_inst(fc7_S)
    D_img_out_S = self.D_img(net_conv_S)

    # loss_D_inst_S = bceLoss_func(D_inst_out_S, Variable(torch.FloatTensor(D_inst_out_S.data.size()).fill_(source_label)).cuda()) 
    loss_D_img_S = bceLoss_func(D_img_out_S, Variable(torch.FloatTensor(D_img_out_S.data.size()).fill_(source_label)).cuda())

    # sig_D_inst_out_S = sig(D_inst_out_S)
    # sig_D_img_out_S = sig(D_img_out_S)
    # mean = sig_D_img_out_S.mean()

    # loss_D_const_S = Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out_S.size()[0]):
    #   loss_D_const_S += torch.dist(mean, sig_D_inst_out_S[j][0])
    # loss_D_const_S /= sig_D_inst_out_S.size()[0]

    loss_D_S = loss_D_img_S / 2.#(loss_D_inst_S + loss_D_img_S + loss_D_const_S) / 2.
    loss_D_S.backward()

    #Target
    # fc7_T = fc7_T.detach()
    net_conv_T = net_conv_T.detach()

    # D_inst_out_T = self.D_inst(fc7_T)
    D_img_out_T = self.D_img(net_conv_T)

    # loss_D_inst_T = bceLoss_func(D_inst_out_T, Variable(torch.FloatTensor(D_inst_out_T.data.size()).fill_(target_label)).cuda()) 
    loss_D_img_T = bceLoss_func(D_img_out_T, Variable(torch.FloatTensor(D_img_out_T.data.size()).fill_(target_label)).cuda())

    # sig_D_inst_out_T = sig(D_inst_out_T)
    # sig_D_img_out_T = sig(D_img_out_T)
    # mean = sig_D_img_out_T.mean()

    # loss_D_const_T = Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out_T.size()[0]):
    #   loss_D_const_T += torch.dist(mean, sig_D_inst_out_T[j][0])
    # loss_D_const_T /= sig_D_inst_out_T.size()[0]

    loss_D_T = loss_D_img_T / 2.#(loss_D_inst_T + loss_D_img_T + loss_D_const_T) / 2.
    loss_D_T.backward()

    #clip gradient
    # clip = 5
    # torch.nn.utils.clip_grad_norm(self.parameters(),clip)

    train_op.step()
    # D_inst_op.step()
    D_img_op.step()

    loss_D_inst_T, loss_D_inst_S, loss_D_inst_adv_T, loss_D_const_adv_T, loss_D_const_T, loss_D_const_S = 0, 0, 0, 0, 0, 0
    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, loss_D_inst_S, loss_D_img_S, loss_D_const_S, loss_D_inst_T, loss_D_img_T, loss_D_const_T, loss_D_inst_adv_T, loss_D_img_adv_T, loss_D_const_adv_T
  
  def train_reconstruct_step(self, blobs_S, blobs_T, train_op, D_inst_op, D_img_op, D_img_branch_op, decoder_op):
    source_label = 0
    target_label = 1
    #utils.timer.timer.tic('backward')
    train_op.zero_grad()
    decoder_op.zero_grad()
    D_img_op.zero_grad()
    # D_img_branch_op.zero_grad()

    content_loss = nn.MSELoss()
    bceLoss_func = nn.BCEWithLogitsLoss()
    diffLoss = DiffLoss()

    fc7, net_conv = self.forward(blobs_S['data'], blobs_S['im_info'], blobs_S['gt_boxes'])

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]
    net_conv_domain = self.domain_feat
    net_conv_add = net_conv + net_conv_domain

    recon = self.decoder(net_conv_add)

    image = cv2.imread(blobs_S['data_path'][0])
    image = image.astype(np.float32, copy=False)
    image = cv2.resize(image, recon.shape[-2:])
    image = image / 255.
    image = Variable(torch.from_numpy(np.array([image.transpose([2,0,1])])).cuda(), volatile=True)

    recon_loss_S = content_loss(recon, image)
    loss_diff_S = diffLoss(net_conv, net_conv_domain)

    net_conv = grad_reverse(net_conv)
    #det loss
    loss_S = self._losses['total_loss']
    #D_inst
    # D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img(net_conv)

    #loss
    # loss_D_inst_S = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(source_label)).cuda()) 
    loss_D_img_S = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(source_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_S =  Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_S += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_S /= sig_D_inst_out.size()[0]

    # D_img_domain_out = self.D_img_domain(self.domain_feat)
    # loss_D_img_domain_S = bceLoss_func(D_img_domain_out, Variable(torch.FloatTensor(D_img_domain_out.data.size()).fill_(source_label)).cuda())
    
    total_loss_S = loss_S + (cfg.ADAPT_LAMBDA/2.) * (loss_D_img_S) + loss_diff_S + recon_loss_S#(loss_D_inst_S + loss_D_img_S + loss_D_const_S)
    # total_loss_S = loss_S + (cfg.ADAPT_LAMBDA/2.) * (loss_D_img_S + loss_D_img_domain_S) + loss_diff_S + recon_loss_S#(loss_D_inst_S + loss_D_img_S + loss_D_const_S)



    fc7, net_conv = self.forward(blobs_T['data'], blobs_T['im_info'], blobs_T['gt_boxes'])

    net_conv_domain = self.domain_feat
    net_conv_add = net_conv + net_conv_domain

    recon = self.decoder(net_conv_add)

    image = cv2.imread(blobs_T['data_path'][0])
    image = image.astype(np.float32, copy=False)
    image = cv2.resize(image, recon.shape[-2:])
    image = image / 255.
    image = Variable(torch.from_numpy(np.array([image.transpose([2,0,1])])).cuda(), volatile=True)

    recon_loss_T = content_loss(recon, image)
    loss_diff_T = diffLoss(net_conv, net_conv_domain)

    net_conv = grad_reverse(net_conv)

    #D_inst
    # D_inst_out = self.D_inst(fc7)
    #D_img
    D_img_out = self.D_img(net_conv)

    #loss
    # loss_D_inst_T = bceLoss_func(D_inst_out, Variable(torch.FloatTensor(D_inst_out.data.size()).fill_(source_label)).cuda()) 
    loss_D_img_T = bceLoss_func(D_img_out, Variable(torch.FloatTensor(D_img_out.data.size()).fill_(target_label)).cuda())

    # sig_D_inst_out = sig(D_inst_out)
    # sig_D_img_out = sig(D_img_out)
    # mean = sig_D_img_out.mean()

    # loss_D_const_T =  Variable(torch.FloatTensor(1).zero_()).cuda()
    # for j in range(sig_D_inst_out.size()[0]):
    #   loss_D_const_T += torch.dist(mean, sig_D_inst_out[j][0])
    # loss_D_const_T /= sig_D_inst_out.size()[0]

    # D_img_domain_out = self.D_img_domain(self.domain_feat)
    # loss_D_img_domain_T = bceLoss_func(D_img_domain_out, Variable(torch.FloatTensor(D_img_domain_out.data.size()).fill_(target_label)).cuda())
    
    total_loss_T = (cfg.ADAPT_LAMBDA/2.) * (loss_D_img_T) + loss_diff_T + recon_loss_T#(loss_D_inst_T + loss_D_img_T + loss_D_const_T)
    # total_loss_T = (cfg.ADAPT_LAMBDA/2.) * (loss_D_img_T + loss_D_img_domain_T) + loss_diff_T + recon_loss_T#(loss_D_inst_T + loss_D_img_T + loss_D_const_T)

    
    loss = total_loss_S + total_loss_T
    loss.backward()
    #utils.timer.timer.toc('backward')
    train_op.step()
    decoder_op.step()
    D_img_op.step()
    # D_img_branch_op.step()

    self.delete_intermediate_states()

    loss_D_inst_S, loss_D_const_S, loss_D_inst_T, loss_D_const_T, loss_D_img_domain_S, loss_D_img_domain_T = 0, 0, 0, 0, 0, 0
    # loss_D_inst_S, loss_D_const_S, loss_D_inst_T, loss_D_const_T = 0, 0, 0, 0
    # loss_D_img_S, loss_D_const_S, loss_D_img_T, loss_D_const_T = 0, 0, 0, 0

    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, loss_D_inst_S, loss_D_img_S, loss_D_const_S, loss_D_inst_T, loss_D_img_T, loss_D_const_T, loss_diff_S, loss_diff_T,\
           loss_D_img_domain_S, loss_D_img_domain_T, recon_loss_S, recon_loss_T

  def train_step(self, blobs, train_op):
    self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'])

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]

    #utils.timer.timer.tic('backward')
    train_op.zero_grad()
    self._losses['total_loss'].backward()
    #utils.timer.timer.toc('backward')
    train_op.step()

    self.delete_intermediate_states()

    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss

  def train_step_subIters(self, blobs, train_op):
    self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'])

    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]

    #utils.timer.timer.tic('backward')
    # train_op.zero_grad()
    (self._losses['total_loss']/2.0).backward() #subIter = 2
    #utils.timer.timer.toc('backward')
    # train_op.step()

    self.delete_intermediate_states()

    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss

  def train_step_with_summary(self, blobs, train_op):
    self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'])
    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]
    train_op.zero_grad()
    self._losses['total_loss'].backward()
    train_op.step()
    summary = self._run_summary_op()

    self.delete_intermediate_states()

    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, summary

  def train_step_no_return(self, blobs, train_op):
    self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'])
    train_op.zero_grad()
    self._losses['total_loss'].backward()
    train_op.step()
    self.delete_intermediate_states()

  def load_state_dict(self, state_dict):
    """
    Because we remove the definition of fc layer in resnet now, it will fail when loading 
    the model trained before.
    To provide back compatibility, we overwrite the load_state_dict
    """
    # dList = []
    # #print(type(self.state_dict()))
    # for d in self.state_dict():
    #   if 'D_inst' not in d and 'D_img' not in d:
    #     dList.append(d)
    #print(dList)
    #print('D_inst.fc1.weight' in dList)
    netDict = self.state_dict()
    # print(netDict.keys())
    stateDict = {k: v for k, v in state_dict.items() if k in netDict}
    netDict.update(stateDict)
    nn.Module.load_state_dict(self, netDict)

    # nn.Module.load_state_dict(self, {k: state_dict[k] for k in list(self.state_dict())})

