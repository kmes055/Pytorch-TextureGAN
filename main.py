import torch
import torch.nn as nn
import torch.optim as optim

import sys, os
import visdom
import torchvision.models as models
from torch.utils.data.sampler import SequentialSampler

from torch.utils.data import DataLoader
from dataloader.imfol import ImageFolder

from utils import transforms as custom_transforms
from models import scribbler, discriminator, texturegan, define_G, weights_init, \
    scribbler_dilate_128, GramMatrix, FeatureExtractor
from train import train
import argparser


# TODO: finetuning DTD
# TODO: unmatch the sketch/texture input patch location for DTD

def get_transforms(args):
    transforms_list = [
        custom_transforms.RandomSizedCrop(args.image_size, args.resize_min, args.resize_max),
        custom_transforms.RandomHorizontalFlip(),
        custom_transforms.toTensor()
    ]
    if args.color_space == 'lab':
        transforms_list.insert(2, custom_transforms.toLAB())
    elif args.color_space == 'rgb':
        transforms_list.insert(2, custom_transforms.toRGB('RGB'))

    transforms = custom_transforms.Compose(transforms_list)
    return transforms


def get_models(args):
    sigmoid_flag = 1
    if args.gan == 'lsgan':
        sigmoid_flag = 0

    if args.model == 'scribbler':
        netG = scribbler.Scribbler(5, 3, 32)
    elif args.model == 'texturegan':
        netG = texturegan.TextureGAN(5, 3, 32)
    elif args.model == 'pix2pix':
        netG = define_G(5, 3, 32)
    elif args.model == 'scribbler_dilate_128':
        netG = scribbler_dilate_128.ScribblerDilate128(5, 3, 32)
    else:
        print(args.model + ' not support. Using Scribbler model')
        netG = scribbler.Scribbler(5, 3, 32)

    if args.color_space == 'lab':
        netD = discriminator.Discriminator(1, 32, sigmoid_flag)
    elif args.color_space == 'rgb':
        netD = discriminator.Discriminator(3, 32, sigmoid_flag)

    if args.load == -1:
        netG.apply(weights_init)
    else:
        load_network(netG, 'G', args.load, args.load_dir)
        print('Loaded G from itr:' + str(args.load))

    if args.load_D == -1:
        netD.apply(weights_init)
    else:
        load_network(netD, 'D', args.load_D, args.load_dir)
        print('Loaded D from itr:' + str(args.load_D))

    return netG, netD


def get_criterions(args):
    if args.gan == 'lsgan':
        criterion_gan = nn.MSELoss()
    elif args.gan == 'dcgan':
        criterion_gan = nn.BCELoss()
    else:
        print("Undefined GAN type. Defaulting to LSGAN")
        criterion_gan = nn.MSELoss()

    # criterion_l1 = nn.L1Loss()
    criterion_pixel_l = nn.L1Loss()
    criterion_pixel_ab = nn.L1Loss()
    criterion_style = nn.L1Loss()
    criterion_feat = nn.L1Loss()

    return criterion_gan, criterion_pixel_l, criterion_pixel_ab, criterion_style, criterion_feat


def main(args):
    layers_map = {'relu4_2': '22', 'relu2_2': '8', 'relu3_2': '13'}

    vis = visdom.Visdom(port=args.display_port)

    loss_graph = {
        "g": [],
        "gd": [],
        "gf": [],
        "gpl": [],
        "gpab": [],
        "gs": [],
        "d": [],
    }

    # for rgb the change is to feed 3 channels to D instead of just 1. and feed 3 channels to vgg.
    # can leave pixel separate between r and gb for now. assume user use the same weights
    transforms = get_transforms(args)

    if args.color_space == 'rgb':
        args.pixel_weight_ab = args.pixel_weight_rgb
        args.pixel_weight_l = args.pixel_weight_rgb

    rgbify = custom_transforms.toRGB()

    train_dataset = ImageFolder('train', args.data_path, transforms)
    train_loader = DataLoader(dataset=train_dataset, batch_size=args.batch_size, shuffle=True)

    val_dataset = ImageFolder('val', args.data_path, transforms)
    indices = torch.randperm(len(val_dataset))
    val_display_size = args.batch_size
    val_display_sampler = SequentialSampler(indices[:val_display_size])
    val_loader = DataLoader(dataset=val_dataset, batch_size=val_display_size, sampler=val_display_sampler)
    # renormalize = transforms.Normalize(mean=[+0.5+0.485, +0.5+0.456, +0.5+0.406], std=[0.229, 0.224, 0.225])

    feat_model = models.vgg19(pretrained=True)
    netG, netD = get_models(args)

    criterion_gan, criterion_pixel_l, criterion_pixel_ab, criterion_style, criterion_feat = get_criterions(args)


    real_label = 1
    fake_label = 0

    optimizerD = optim.Adam(netD.parameters(), lr=args.learning_rate_D, betas=(0.5, 0.999))
    optimizerG = optim.Adam(netG.parameters(), lr=args.learning_rate, betas=(0.5, 0.999))

    with torch.cuda.device(args.gpu):
        netG.cuda()
        netD.cuda()
        feat_model.cuda()
        criterion_gan.cuda()
        criterion_pixel_l.cuda()
        criterion_pixel_ab.cuda()
        criterion_feat.cuda()

        input_stack = torch.FloatTensor().cuda()
        target_img = torch.FloatTensor().cuda()
        target_texture = torch.FloatTensor().cuda()
        segment = torch.FloatTensor().cuda()
        label = torch.FloatTensor(args.batch_size).cuda()

        extract_content = FeatureExtractor(feat_model.features, [layers_map[args.content_layers]])
        extract_style = FeatureExtractor(feat_model.features,
                                         [layers_map[x.strip()] for x in args.style_layers.split(',')])

        model = {
            "netG": netG,
            "netD": netD,
            "criterion_gan": criterion_gan,
            "criterion_pixel_l": criterion_pixel_l,
            "criterion_pixel_ab": criterion_pixel_ab,
            "criterion_feat": criterion_feat,
            "criterion_style": criterion_style,
            "real_label": real_label,
            "fake_label": fake_label,
            "optimizerD": optimizerD,
            "optimizerG": optimizerG
        }

        for epoch in range(args.num_epoch):
            train(model, train_loader, val_loader, input_stack, target_img, target_texture,
                  segment, label, extract_content, extract_style, loss_graph, vis, args)



def save_network(model, network_label, epoch_label, gpu_id, save_dir):
    save_filename = '%s_net_%s.pth' % (epoch_label, network_label)
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    save_path = os.path.join(save_dir, save_filename)
    torch.save(model.cpu().state_dict(), save_path)
    model.cuda(device_id=gpu_id)


def load_network(model, network_label, epoch_label, save_dir):
    save_filename = '%s_net_%s.pth' % (epoch_label, network_label)
    save_path = os.path.join(save_dir, save_filename)
    model.load_state_dict(torch.load(save_path))


if __name__ == '__main__':
    args = argparser.parse_arguments()
    main(args)