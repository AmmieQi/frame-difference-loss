"""
Train the SFN baseline model (without temporal loss)
"""
import numpy as np
import argparse, sys, os, time, glob
import torch
from torch.functional import F
from torch.utils.data import DataLoader
from torchvision.datasets import ImageFolder
import dataset
from torchvision import transforms
from torch.functional import F
import utils
import transformer_net
from vgg16 import Vgg16

def center_crop(x, h, w):
    # Assume x is (N, C, H, W)
    H, W = x.size()[2:]
    if h == H and w == W: return x
    assert(h <= H and w <= W)
    dh, dw = H - h, W - w
    ddh, ddw = dh // 2, dw // 2
    return x[:, :, ddh:H-ddh, ddw:W-ddw]


def train(args):
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    if args.cuda:
        torch.cuda.manual_seed(args.seed)
        kwargs = {'num_workers': 0, 'pin_memory': False}
    else:
        kwargs = {}

    if args.model_type == "rnn":
        transformer = transformer_net.TransformerRNN(args.pad_type)
        seq_size = 4
    else:
        transformer = transformer_net.TransformerNet(args.pad_type)
        seq_size = 2

    train_dataset = dataset.DAVISDataset(args.dataset,
        seq_size=seq_size, interval=args.interval, use_flow=False)
    train_loader = DataLoader(train_dataset, batch_size=1, **kwargs)

    models = glob.glob(args.init_model_dir + "/epoch*.model")
    models.sort()
    model_path = models[-1]
    print("=> Load from model file %s" % model_path)
    transformer.load_state_dict(torch.load(model_path))
    transformer.train()
    if args.model_type == "rnn":
        transformer.conv1 = transformer_net.ConvLayer(6, 32, kernel_size=9, stride=1, pad_type=args.pad_type)
    optimizer = torch.optim.Adam(transformer.parameters(), args.lr)
    mse_loss = torch.nn.MSELoss()
    l1_loss = torch.nn.SmoothL1Loss()

    vgg = Vgg16()
    utils.init_vgg16(args.vgg_model_dir)
    vgg.load_state_dict(torch.load(os.path.join(args.vgg_model_dir, "vgg16.weight")))
    vgg.eval()

    if args.cuda:
        transformer.cuda()
        vgg.cuda()
        mse_loss.cuda()
        l1_loss.cuda()

    style = utils.tensor_load_resize(args.style_image, args.style_size)
    style = style.unsqueeze(0)
    print("=> Style image size: " + str(style.size()))
    print("=> Pixel FDB loss weight: %f" % args.time_strength1)
    print("=> Feature FDB loss weight: %f" % args.time_strength2)

    style = utils.preprocess_batch(style)
    if args.cuda: style = style.cuda()
    utils.tensor_save_bgrimage(style[0].detach(), os.path.join(args.save_model_dir, 'train_style.jpg'), args.cuda)
    style = utils.subtract_imagenet_mean_batch(style)
    features_style = vgg(style)
    gram_style = [utils.gram_matrix(y).detach() for y in features_style]

    for e in range(args.epochs):
        train_loader.dataset.reset()
        agg_content_loss = agg_style_loss = agg_pixelfdb_loss = agg_featurefdb_loss = 0.
        iters = 0
        anormaly = False
        for batch_id, (x, flow, conf) in enumerate(train_loader):
            x=x[0]
            iters += 1

            optimizer.zero_grad()
            x = utils.preprocess_batch(x) # (N, 3, 256, 256)
            if args.cuda: x = x.cuda()
            y = transformer(x) # (N, 3, 256, 256)

            if (batch_id + 1) % 100 == 0:
                idx = (batch_id + 1) // 100
                for i in range(args.batch_size):
                    utils.tensor_save_bgrimage(y.data[i],
                        os.path.join(args.save_model_dir, "out_%02d_%02d.png" % (idx, i)),
                        args.cuda)
                    utils.tensor_save_bgrimage(x.data[i],
                        os.path.join(args.save_model_dir, "in_%02d-%02d.png" % (idx, i)),
                        args.cuda)

            xc = center_crop(x.detach(), y.shape[2], y.shape[3])

            y = utils.subtract_imagenet_mean_batch(y)
            xc = utils.subtract_imagenet_mean_batch(xc)

            features_y = vgg(y)
            features_xc = vgg(xc)
            
            #content target
            f_xc_c = features_xc[2].detach()
            # content
            f_c = features_y[2]

            content_loss = args.content_weight * mse_loss(f_c, f_xc_c)

            style_loss = 0.
            for m in range(len(features_y)):
                gram_s = gram_style[m]
                gram_y = utils.gram_matrix(features_y[m])
                batch_style_loss = 0
                for n in range(gram_y.shape[0]):
                    batch_style_loss += args.style_weight * mse_loss(gram_y[n], gram_s[0])
                style_loss += batch_style_loss / gram_y.shape[0]
                
            # FDB
            pixel_fdb_loss = args.time_strength1 * mse_loss(y[1:] - y[:-1], xc[1:] - xc[:-1])
            # temporal content: 16th
            feature_fdb_loss = args.time_strength2 * l1_loss(
                features_y[2][1:] - features_y[2][:-1],
                features_xc[2][1:] - features_xc[2][:-1])

            total_loss = content_loss + style_loss + pixel_fdb_loss + feature_fdb_loss

            total_loss.backward()
            optimizer.step()

            agg_content_loss += content_loss.data
            agg_style_loss += style_loss.data
            agg_pixelfdb_loss += pixel_fdb_loss.data
            agg_featurefdb_loss += feature_fdb_loss.data

            agg_total = agg_content_loss + agg_style_loss + agg_pixelfdb_loss + agg_featurefdb_loss
            mesg = "{}\tEpoch {}:\t[{}/{}]\tcontent: {:.6f}\tstyle: {:.6f}\tpixel fdb: {:.6f}\tfeature fdb: {:.6f}\ttotal: {:.6f}".format(
                time.ctime(), e + 1, batch_id + 1, len(train_loader),
                            agg_content_loss / iters,
                            agg_style_loss / iters,
                            agg_pixelfdb_loss / iters,
                            agg_featurefdb_loss / iters,
                            agg_total / iters)
            print(mesg)
            agg_content_loss = agg_style_loss = agg_pixelfdb_loss = agg_featurefdb_loss = 0.0
            iters = 0

        # save model
        save_model_filename = "epoch_" + str(e) + "_" + str(args.content_weight) + "_" + str(args.style_weight) + ".model"
        save_model_path = os.path.join(args.save_model_dir, save_model_filename)
        torch.save(transformer.state_dict(), save_model_path)

    print("\nDone, trained model saved at", save_model_path)


def check_paths(args):
    try:
        if not os.path.exists(args.vgg_model_dir):
            os.makedirs(args.vgg_model_dir)
        if not os.path.exists(args.save_model_dir):
            os.makedirs(args.save_model_dir)
    except OSError as e:
        print(e)
        sys.exit(1)

def stylize_multiple(args):
    pad_types = args.pad_type.split(",")
    load_paths = []
    model_names = []
    flags = []
    for pad_type in pad_types:
        expr_dirs = os.listdir(os.path.join(args.model_dir, pad_type))
        expr_dirs.sort()
        for expr_dir in expr_dirs:
            models = glob.glob(os.path.join(args.model_dir, pad_type, expr_dir, "epoch*.model"))
            models.sort()
            print(os.path.join(args.model_dir, pad_type, expr_dir, "epoch*.model"))
            try:
                load_path = models[-1]
            except IndexError:
                continue
            ind = load_path.find("Style")
            sery = int(load_path[ind + len("Style"):][0:1])
            model_name = "diff"
            model_name = "sfn_" + model_name + "_" + pad_type + "_" + str(sery)
            print("=> Record model file %s - %s" % (load_path, model_name))
            load_paths.append(load_path)
            model_names.append(model_name)
            flags.append(dict(pad_type=pad_type))
    
    transform = transforms.Compose([
        transforms.Resize(256),
        transforms.ToTensor(),
        transforms.Lambda(lambda x: x.mul(255))])
    ds = ImageFolder(args.input_dir, transform=transform)
    dl = DataLoader(ds, batch_size=1)

    for n,m,flag in zip(model_names, load_paths, flags):
        args.model_name = n
        if args.compute:
            net = transformer_net.TransformerNet(**flag)
            net.load_state_dict(torch.load(m))
            net = net.eval().cuda()
            print(net)
            utils.process_dataloader(args, net, dl)
            del net
        utils.generate_video(args, dl)

def stylize(args):
  if ',' in args.pad_type:
    stylize_multiple(args)
    return

  transform = transforms.Compose([
        transforms.Resize(256),
        transforms.ToTensor(),
        transforms.Lambda(lambda x: x.mul(255))])
  try:
    ds = ImageFolder(args.input_dir, transform=transform)
  except:
    ds = dataset.CustomImageDataset(args.input_dir, img_size=None, transform=transform, shuffle=False)
  dl = DataLoader(ds, batch_size=1)
  models = glob.glob(args.model_dir + "/epoch*.model")
  models.sort()
  model_path = models[-1]
  print("=> Load from model file %s" % model_path)
  if args.model_type == "rnn":
    net = transformer_net.TransformerRNN(args.pad_type)
    net.conv1 = transformer_net.ConvLayer(6, 32, 9, 1, pad_type=args.pad_type)
  else:
    net = transformer_net.TransformerNet(args.pad_type)
  net.load_state_dict(torch.load(model_path))
  net = net.eval().cuda()
  print(net)
  
  if args.compute:
    utils.process_dataloader(args, net, dl)
  utils.generate_video(args, dl)

def main():
    main_arg_parser = argparse.ArgumentParser(description="parser for Frame Difference-Based (FDB) Temporal Loss experiments.")
    subparsers = main_arg_parser.add_subparsers(
        title="subcommands", dest="subcommand")

    train_arg_parser = subparsers.add_parser("train",
        help="parser for training arguments")
    # loss
    train_arg_parser.add_argument("--time-strength1",
        type=float, default=50.0,
        help="pixel FDB weight")
    train_arg_parser.add_argument("--time-strength2",
        type=float, default=950.0,
        help="feature FDB weight")
    train_arg_parser.add_argument("--content-weight",
        type=float, default=1.0,
        help="weight for content-loss, default is 1.0")
    train_arg_parser.add_argument("--style-weight",
        type=float, default=10.0,
        help="weight for style-loss, default is 10.0")
    # paths
    train_arg_parser.add_argument("--dataset",
        type=str, default="data/DAVIS/train/JPEGImages/480p/",
        help="path to the DAVIS dataset")
    train_arg_parser.add_argument("--init-model-dir",
        type=str, default="exprs/NetStyle1/",
        help="model dir")
    train_arg_parser.add_argument("--vgg-model-dir",
        type=str, default="pretrained/",
        help="directory for vgg, if model is not present in the directory it is downloaded")
    train_arg_parser.add_argument("--save-model-dir",
        type=str, default="exprs/DiffStyle1",
        help="path to folder where trained model will be saved.")
    train_arg_parser.add_argument("--style-image",
        type=str, default="data/styles/starry_night.jpg",
        help="path to style-image")
    # model config
    train_arg_parser.add_argument("--model-type",
        type=str, default="sfn", 
        help="model dir")
    train_arg_parser.add_argument("--pad-type",
        type=str, default="interpolate-detach",
        help="interpolate-detach (default) | reflect-start | none | reflect | replicate | zero")
    # training config
    train_arg_parser.add_argument("--epochs",
        type=int, default=1,
        help="number of training epochs, default is 2.")
    train_arg_parser.add_argument("--batch-size",
        type=int, default=2,
        help="batch size for training, default is 2.")
    train_arg_parser.add_argument("--interval",
        type=int, default=1,
        help="Frame interval. Default is 1.")
    train_arg_parser.add_argument("--image-size",
        type=int, default=400,
        help="size of training images, default is 400 X 400")
    train_arg_parser.add_argument("--style-size",
        type=int, default=400,
        help="size of style-image, default is the original size of style image")
    train_arg_parser.add_argument("--seed",
        type=int, default=1234,
        help="random seed for training")
    train_arg_parser.add_argument("--lr",
        type=float, default=1e-4,
        help="learning rate, default is 0.001")

    eval_arg_parser = subparsers.add_parser("eval", help="parser for evaluation/stylizing arguments")
    # model config
    eval_arg_parser.add_argument("--model-type",
        type=str, default="sfn",
        help="sfn | rnn")
    eval_arg_parser.add_argument("--pad-type",
        type=str, default="interpolate-detach",
        help="interpolate-detach (default) | reflect-start | none | reflect | replicate | zero")
    # paths
    eval_arg_parser.add_argument("--input-dir",
        type=str, default="data/test",
        help="Standard dataset: video_name1/*.jpg video_name2/*.jpg")
    eval_arg_parser.add_argument("--output-dir",
        type=str, default="/home/xujianjing/testdataout",
        help="The output directory of generated images.")
    eval_arg_parser.add_argument("--model-dir",
        type=str, default="exprs/CombStyle1/",
        help="The path to saved model dir.")
    # others
    eval_arg_parser.add_argument("--model-name",
        type=str, default="sfn_comb_starrynight",
        help="The model name (used in naming output videos).")
    eval_arg_parser.add_argument("--compute",
        type=int, default=1,
        help="Whether to generate new images or use existing ones.")

    args = main_arg_parser.parse_args()

    if args.subcommand is None:
        print("ERROR: specify either train or eval")
        sys.exit(1)

    if args.subcommand == "train":
        check_paths(args)
        train(args)
    else:
        stylize(args)

if __name__ == "__main__":
    main()
