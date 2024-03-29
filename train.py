import os
os.environ["CUDA_VISIBLE_DEVICES"] = "2,3,4,5"
import random

import torch
import math
import numpy as np
import timm
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.utils.data.distributed
import torch.optim.lr_scheduler as lr_scheduler
from torch.nn.parallel import DistributedDataParallel as DDP
from ignite.handlers import create_lr_scheduler_with_warmup
from tensorboardX import SummaryWriter
# from torchvision.transforms import Compose, Resize, ToTensor, ToPILImage
from torch.cuda import amp
from utils.dataset import ImgDataSet
from utils.utils import read_spilt_data, get_loader, train_one_epoch, evaluate
from utils.parser import parser_args


# set seed
def init(seed):
    seed = seed
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.deterministic = True

def cleanup():
    dist.destroy_process_group()

def is_main_worker(gpu):
    return (gpu <= 0)

# mp.spawn will pass the value "gpu" as rank
def train_ddp(rank, world_size, args_dict):

    port = args_dict['port']
    dist.init_process_group(
        backend='nccl',
        init_method="tcp://127.0.0.1:" + str(port),
        world_size=world_size,
        rank=rank,
    )

    train(args_dict, ddp_gpu=rank)
    cleanup()

# train function
def train(args_dict, ddp_gpu=-1):
    cudnn.benchmark = True

    # set gpu of each multiprocessing
    torch.cuda.set_device(ddp_gpu)
    
    # get dataLoader
    train_loader, val_loader = get_loader(args_dict) 
    print("Get data loader successfully")

    # scheduler = lr_scheduler.StepLR(opt, step_size=3, gamma=0.1)

    ### import timm model, see more in model.txt ###

    # swin_large_patch4_window7_224 # swin transformer 
    # convnext_large # convnext
    # vit_large_patch32_224 # vit
    # vit_base_resnet50d_224 # hybrid model
    # deit_base_distilled_patch16_224 # deit
    # efficientnetv2_m
    # efficientnetv2_rw_m
    # efficientnetv2_rw_s
    # efficientnetv2_rw_t
    # gc_efficientnetv2_rw_t
    # efficientnetv2_s
    # efficientformerv2_s0, efficientformerv2_s1, efficientformerv2_s2, 
    model = timm.create_model('swin_base_patch4_window7_224', pretrained=False, num_classes=args_dict['n_classes'])

    # check if folder exist and start summarywriter on main worker
    if is_main_worker(ddp_gpu):
        print("Start Training")
        if not os.path.exists(args_dict['model_save_path']):
            os.mkdir(args_dict['model_save_path'])
        tb_writer = SummaryWriter(args_dict['model_save_path'])

        # save the whole model at first time to avoid loss model
        # print("Save init model")
        # save_path = os.path.join(args_dict['model_save_path'], "init_model.pt")
        
        # torch.save(model, save_path)

    # setting optim
    pg = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.SGD(pg, lr=args_dict['lr'], momentum=args_dict['momentum'], weight_decay=args_dict['weight_decay'])
    
    # setting lr scheduler as cosine annealing
    lf = lambda x: ((1 + math.cos(x * math.pi / args_dict['cosanneal_cycle'])) / 2) * (1 - args_dict['lrf']) + args_dict['lrf']
    scheduler = lr_scheduler.LambdaLR(optimizer=opt, lr_lambda=lf)
    
    # setting warm up info
    if args_dict['warmup'] :
        warmup = create_lr_scheduler_with_warmup(
            scheduler, 
            warmup_start_value=args_dict['warmup_start_value'],
            warmup_end_value=args_dict['lr'],
            warmup_duration=args_dict['warmup_step'],
        )

    start_epoch = 0


    # setting Distributed 
    if args_dict['use_ddp']:   
        model = DDP(model.to(ddp_gpu))
        # model = DDP(model(args_dict).to(ddp_gpu))

    model = model.to(ddp_gpu)

    # # check if folder exist and start summarywriter on main worker
    # if is_main_worker(ddp_gpu):
    #     print("Start Training")
    #     if not os.path.exists(args_dict['model_save_path']):
    #         os.mkdir(args_dict['model_save_path'])
    #     tb_writer = SummaryWriter(args_dict['model_save_path'])

    #     # save the whole model at first time to avoid loss model
    #     print("Save init model")
    #     save_path = args_dict['model_save_path'] + "init_model.pt"
    #     tmp_model = model
    #     torch.save(tmp_model, save_path)

    # if need load model and keep training
    # if args_dict['load_state']:
    #     model.load_state_dict(torch.load(args_dict['resume_model_path']), strict=True)
    #     print("Load model successfully")
    # else:
    #     model = model.to(ddp_gpu)

    # setting the next training epoch of loading model
    # if args_dict['skip_epoch'] >= 0 and args_dict['load_state']:
    #     start_epoch = args_dict['skip_epoch'] + 1
    #     if epoch < args_dict['warmup_step']:
    #         warmup(None)
    #     else:
    #         scheduler.step()
    # else:
    #     start_epoch = 0

    
    
    score = 0
    
    # setting Automatic mixed precision
    scaler = amp.GradScaler()

    # start training
    for epoch in range(start_epoch, args_dict['epoch']):
        
        # train 
        train_loss, train_acc = train_one_epoch(
            model=model, 
            optimizer=opt,
            data_loader=train_loader,
            device=ddp_gpu,
            epoch=epoch,
            scaler=scaler,
            args_dict=args_dict
        )

        # update scheduler 
        if args_dict['warmup']:
            warmup(None)
        else:
            scheduler.step()

        # eval
        val_loss, val_acc = evaluate(
            model=model, 
            data_loader=val_loader,
            device=ddp_gpu,
            epoch=epoch,
            classes=args_dict['n_classes']
        )

        # write info into summarywriter in main worker
        if is_main_worker(ddp_gpu):
            tags = ["train_loss", "train_acc", "val_loss", "val_acc", "lr"]
            tb_writer.add_scalar(tags[0], train_loss, epoch)
            tb_writer.add_scalar(tags[1], train_acc, epoch)
            tb_writer.add_scalar(tags[2], val_loss, epoch)
            tb_writer.add_scalar(tags[3], val_acc, epoch)
            tb_writer.add_scalar(tags[4], opt.param_groups[0]['lr'], epoch)

            # save model every two epoch 
            if (epoch % args_dict['save_frequency'] == 0 and epoch >= 10):
                save_path = os.path.join(args_dict['model_save_path'], "model_{}_{:.3f}_.pth".format(epoch, val_acc))
                torch.save(model, save_path)
            elif (epoch >= 10 and score < val_acc):
                save_path = os.path.join(args_dict['model_save_path'], "model_{}_{:.3f}_.pth".format(epoch, val_acc))
                torch.save(model, save_path)
                score = val_acc

if __name__ == '__main__':

    # get args
    args = parser_args()
    args_dict = vars(args)

    init(args_dict['seed'])

    # train in ddp or not
    if args_dict['use_ddp']:
        n_gpus_per_node = torch.cuda.device_count()
        world_size = n_gpus_per_node
        mp.spawn(train_ddp, nprocs=n_gpus_per_node, args=(world_size, args_dict))
    else:
        train(args_dict)

    # model = model(args_dict)
    # print(model.__dict__)
    

    