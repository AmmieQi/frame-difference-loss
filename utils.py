"""
Utilization script.
"""
import matplotlib
matplotlib.use("agg")
import matplotlib.pyplot as plt
import os, torch
import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image
from torch.autograd import Variable
from vgg16 import Vgg16


def read_flow_file(path):
    """
    Read the .flo file generated by DeepFlow
    """
    with open(path, 'rb') as f:
        info = np.fromfile(f, np.int32, 3)
    W, H = info[1:]
    with open(path, 'rb') as f:
        raw = np.fromfile(f, np.float32)
        raw = raw[3:].reshape(H, W, 2)
    return raw


def read_image_file(path):
    # open path as file to avoid ResourceWarning (https://github.com/python-pillow/Pillow/issues/835)
    with open(path, 'rb') as f:
        return np.array(Image.open(f))


def save_image(name, nparr):
    if nparr.shape[-1] == 1:
        nparr = nparr[:, :, 0]
    # save uint8 arr as image
    with open(name, 'wb') as f:
        return Image.fromarray(nparr).save(f, format="PNG")


def tensor_save_image(filename, tensor):
    if tensor.max() < 2:
        if tensor.min() < -0.5:
            # -1 ~ 1 scale
            nparr = (tensor * 127.5 + 127.5).detach().cpu().numpy()
        else:
            # 0 ~ 1  scale
            nparr = (tensor * 255.).detach().cpu().numpy()
    else:
        nparr = tensor.detach().cpu().numpy()
    
    if nparr.shape[0] == 1: # squeeze channel dim
        nparr = nparr[0]

    if len(nparr.shape) > 2: # transpose channel to last
        nparr = nparr.transpose(1, 2, 0)

    save_image(filename, nparr.astype("uint8"))


def tensor_load_resize(filename, size=0):
    img = Image.open(filename)
    w, h = img.size
    if size:
        if h < w:
            w_ = int(size / float(h) * w)
            img = img.resize((w_, size), Image.ANTIALIAS)
        else:
            h_ = int(size / float(w) * h)
            img = img.resize((size, h_), Image.ANTIALIAS)
        
    img = np.array(img).transpose(2, 0, 1)
    img = torch.from_numpy(img).float()
    return img


def tensor_load_rgbimage(filename, size=None, scale=None):
    img = Image.open(filename)
    if size is not None:
        img = img.resize((size, size), Image.ANTIALIAS)
    elif scale is not None:
        img = img.resize((int(img.size[0] / scale), int(img.size[1] / scale)), Image.ANTIALIAS)
    img = np.array(img).transpose(2, 0, 1)
    img = torch.from_numpy(img).float()
    return img


def tensor_save_rgbimage(tensor, filename, cuda=False):
    if cuda:
        img = tensor.clone().cpu().clamp(0, 255).numpy()
    else:
        img = tensor.clone().clamp(0, 255).numpy()
    img = img.transpose(1, 2, 0).astype('uint8')
    img = Image.fromarray(img)
    img.save(filename)


def tensor_save_bgrimage(tensor, filename, cuda=False):
    (b, g, r) = torch.chunk(tensor, 3)
    tensor = torch.cat((r, g, b))
    tensor_save_rgbimage(tensor, filename, cuda)


def gram_matrix(y):
    (b, ch, h, w) = y.size()
    features = y.view(b, ch, w * h)
    features_t = features.transpose(1, 2)
    gram = features.bmm(features_t) / (ch * h * w)
    return gram


def subtract_imagenet_mean_batch(batch):
    mean = torch.Tensor([103.939, 116.779, 123.680])
    mean = mean.cuda().view(1, 3, 1, 1)
    return batch - mean


def preprocess_batch(batch):
    batch = batch.transpose(0, 1)
    (r, g, b) = torch.chunk(batch, 3)
    batch = torch.cat((b, g, r))
    batch = batch.transpose(0, 1)
    return batch


def process_dataloader(args, net, dl):
  prev_dir_name = ""
  # generate image
  for idx, (x, _) in enumerate(dl):
    x = preprocess_batch(x).cuda()
    # in case input has indivisible shape
    if x.size(2) % 4 != 0 or x.size(3) % 4 != 0:
      diff_h = (4 - x.size(2) % 4) % 4
      diff_w = (4 - x.size(3) % 4) % 4
      h1 = diff_h // 2
      h2 = diff_h - h1
      w1 = diff_w // 2
      w2 = diff_w - w1
      x = F.pad(x, (w1, w2, h1, h2), 'reflect')
    y = net(x)
    try:
      img_path = dl.dataset.samples[idx][0]
      ind = img_path.rfind("/")
      out_base = img_path[:ind]
      out_name = img_path[ind+1:]
    except AttributeError:
      out_base = dl.dataset.data_dir
      out_name = dl.dataset.filelist[idx]
    out_base = out_base.replace(args.input_dir, args.output_dir)
    out_path = os.path.join(out_base, args.model_name, out_name)
    print("=> Write output image to %s" % out_path)
    ind = out_path.rfind("/")
    # in case the directory has not been built
    if prev_dir_name != out_path[:ind]:
      os.system("mkdir " + out_base)
      os.system("mkdir " + out_path[:ind])
      prev_dir_name = out_path[:ind]
    if x.size(2) % 4 != 0 or x.size(3) % 4 != 0:
      y = F.pad(x, (-w1, -w2, -h1, -h2), 'reflect')
    tensor_save_bgrimage(y.data[0], out_path, True)


def generate_video(args, dl):
  prev_dir_name = ""
  for idx, (x, _) in enumerate(dl):
    try:
      img_path = dl.dataset.samples[idx][0]
      ind = img_path.rfind("/")
      out_base = img_path[:ind]
      out_name = img_path[ind+1:]
    except AttributeError:
      out_base = dl.dataset.data_dir
      out_name = dl.dataset.filelist[idx]
    out_base = out_base.replace(args.input_dir, args.output_dir)
    out_path = os.path.join(out_base, args.model_name, out_name)
    ind = out_path.rfind("/")
    if prev_dir_name != out_path[:ind]:
      prev_dir_name = out_path[:ind]
      basecmd = "ffmpeg -y -f image2 -i %s -vcodec libx264 -pix_fmt yuv420p -b:v 16000k -vf \"scale=trunc(iw/2)*2:trunc(ih/2)*2\" %s"
      input_video_format = os.path.join(out_base, args.model_name, "frame_%04d.png")
      output_video_path = os.path.join(out_base, args.model_name, "stylized.mp4")
      cmd = basecmd % (input_video_format, output_video_path)
      print(cmd)
      os.system(cmd)
      input_video_format = os.path.join(out_base, args.model_name, "%05d.jpg")
      cmd = basecmd % (input_video_format, output_video_path)
      print(cmd)
      os.system(cmd)
      video_name = img_path.split("/")[-2]
      target_video_path = os.path.join("download/", video_name + "_" + args.model_name + ".mp4")
      cmd = "cp %s %s" % (output_video_path, target_video_path)
      print(cmd)
      os.system(cmd)