import numpy as np
import random
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data.sampler import SubsetRandomSampler
from torch.utils.data import DataLoader
import sklearn.metrics as metrics
import argparse
import ot
import copy
import utils.log
import wandb
from PointDA.data.dataloader import ScanNet, ModelNet, ShapeNet, label_to_idx
from PointDA.Models import PointNet, DGCNN
from utils import pc_utils
from DefRec_and_PCM import DefRec, PCM

from PointDA.Samplers import BalancedSubsetBatchSampler

import tqdm.auto as tqdm
import wandb


NWORKERS=20
MAX_LOSS = 9 * (10**9)

def str2bool(v):
    """
    Input:
        v - string
    output:
        True/False
    """
    if isinstance(v, bool):
       return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

# ==================
# Argparse
# ==================
parser = argparse.ArgumentParser(description='DA on Point Clouds')
parser.add_argument('--exp_name', type=str, default='DefRec_PCM',  help='Name of the experiment')
parser.add_argument('--out_path', type=str, default='./experiments', help='log folder path')
parser.add_argument('--dataroot', type=str, default='./data', metavar='N', help='data path')
parser.add_argument('--src_dataset', type=str, default='shapenet', choices=['modelnet', 'shapenet', 'scannet'])
parser.add_argument('--trgt_dataset', type=str, default='scannet', choices=['modelnet', 'shapenet', 'scannet'])
parser.add_argument('--epochs', type=int, default=150, help='number of episode to train')
parser.add_argument('--model', type=str, default='dgcnn', choices=['pointnet', 'dgcnn'], help='Model to use')
parser.add_argument('--seed', type=int, default=1, help='random seed (default: 1)')
parser.add_argument('--gpus', type=lambda s: [int(item.strip()) for item in s.split(',')], default='0',
                    help='comma delimited of gpu ids to use. Use "-1" for cpu usage')
parser.add_argument('--DefRec_dist', type=str, default='volume_based_voxels', metavar='N',
                    choices=['volume_based_voxels', 'volume_based_radius'],
                    help='distortion of points')
parser.add_argument('--num_regions', type=int, default=3, help='number of regions to split shape by')
parser.add_argument('--DefRec_on_src', type=str2bool, default=False, help='Using DefRec in source')
parser.add_argument('--apply_PCM', type=str2bool, default=True, help='Using mixup in source')
parser.add_argument('--batch_size', type=int, default=32, metavar='batch_size', help='Size of train batch per domain')
parser.add_argument('--test_batch_size', type=int, default=32, metavar='batch_size', help='Size of test batch per domain')
parser.add_argument('--optimizer', type=str, default='ADAM', choices=['ADAM', 'SGD'])
parser.add_argument('--DefRec_weight', type=float, default=0.5, help='weight of the DefRec loss')
parser.add_argument('--mixup_params', type=float, default=1.0, help='a,b in beta distribution')
parser.add_argument('--lr', type=float, default=2e-4, help='learning rate')
parser.add_argument('--momentum', type=float, default=0.9, help='SGD momentum')
parser.add_argument('--wd', type=float, default=5e-5, help='weight decay')
parser.add_argument('--dropout', type=float, default=0.5, help='dropout rate')
parser.add_argument('--supervised', type=str2bool, default=True, help='run supervised')
parser.add_argument('--softmax', type=str2bool, default=False, help='use softmax')
parser.add_argument('--use_DeepJDOT', type=str2bool, default=True, help='Use DeepJDOT')
parser.add_argument('--DeepJDOT_head', type=str2bool, default=False, help='Another head for DeepJDOT')
parser.add_argument('--DefRec_on_trgt', type=str2bool, default=True, help='Using DefRec in source')
parser.add_argument('--DeepJDOT_classifier', type=str2bool, default=False, help='Using JDOT head for classification')
parser.add_argument('--jdot_alpha', type=float, default=0.001, help='JDOT Alpha')
parser.add_argument('--jdot_sloss', type=float, default=1.0, help='JDOT Weight for Source Classification')
parser.add_argument('--jdot_tloss', type=float, default=0.0001, help='JDOT Weight for Target Classification')
parser.add_argument('--jdot_train_cl', type=float, default=1.0, help='JDOT Train CL')
parser.add_argument('--jdot_train_algn', type=float, default=1.0, help='JDOT Train CL')
parser.add_argument('--use_sigmoid', type=str2bool, default=True, help='Use SIGMOID for the embedding layer of DeepJDOT')
parser.add_argument('--balance_dataset', type=str2bool, default=False, help='Balance Dataset to have equal number from each class')
args = parser.parse_args()


# 1. Start a new run
wandb.init(project='pcc-ablations', entity='pcc-team')   

# config = {
#     'lr': args.lr,
#     'optimizer': args.optimizer,
#     'softmax': args.softmax,
#     'jdot_alpha': args.jdot_alpha,
#     'jdot_sloss': args.jdot_sloss,
#     'jdot_tloss': args.jdot_tloss,
#     'jdot_train_cl': args.jdot_train_cl
# }

# # Maybe this line has to be commented if not running a sweep
# config = wandb.config


def classifier_cat_loss(source_ypred, ypred_t, ys, gamma):
    '''
    classifier loss based on categorical cross entropy in the target domain
    y_true:  
    y_pred: pytorch tensor which has gradients
    
    0:batch_size - is source samples
    batch_size:end - is target samples
    gamma - is the optimal transport plan
    '''   
    # pytorch has the mean-inbuilt, 
    source_loss = torch.nn.functional.cross_entropy(source_ypred,ys)

    ys_cat = torch.nn.functional.one_hot(ys, num_classes=10).type(ypred_t.dtype) 
    
    # categorical cross entropy loss
    #ypred_t = torch.log(ypred_t)
    ypred_t = torch.nn.functional.log_softmax(ypred_t, dim=-1)

    # loss calculation based on double sum (sum_ij (ys^i, ypred_t^j))
    loss = -torch.matmul(ys_cat, torch.transpose(ypred_t,1,0))
    # returns source loss + target loss
    
    # todo: check function of tloss train_cl, and sloss
    return args.jdot_train_cl * (args.jdot_tloss * torch.sum(gamma * loss) + args.jdot_sloss * source_loss)

def softmax_loss(ys, ypred_t):
    '''
    classifier loss based on categorical cross entropy in the target domain
    y_true:  
    y_pred: pytorch tensor which has gradients
    
    0:batch_size - is source samples
    batch_size:end - is target samples
    gamma - is the optimal transport plan
    '''
    ys_cat = torch.nn.functional.one_hot(ys, num_classes=10).type(ypred_t.dtype)
    
    # categorical cross entropy loss
    ypred_t = torch.log(ypred_t)
    #ypred_t = torch.nn.functional.log_softmax(ypred_t, dim=-1)

    # loss calculation based on double sum (sum_ij (ys^i, ypred_t^j))
    loss = -torch.matmul(ys_cat, torch.transpose(ypred_t,1,0))

    return loss

# L2 distance
def L2_dist(x,y):
    '''
    compute the squared L2 distance between two matrics
    '''
    distx = torch.reshape(torch.sum(torch.square(x),1), (-1,1))
    disty = torch.reshape(torch.sum(torch.square(y),1), (1,-1))
    dist = distx + disty
    dist -= 2.0*torch.matmul(x, torch.transpose(y,0,1))  
    return dist
    
# feature allignment loss
def align_loss(g_source, g_target, gamma):
    '''
    source and target alignment loss in the intermediate layers of the target model
    allignment is performed in the target model (both source and target features are from target model)
    y-pred - is the value of intermediate layers in the target model
    1:batch_size - is source samples
    batch_size:end - is target samples 
    gamma - ot parameter
    '''
    # source domain features            
    #gs = y_pred[:batch_size,:] # this should not work????
    # target domain features
    #gt = y_pred[batch_size:,:]
    gdist = L2_dist(g_source,g_target)  
    return args.jdot_train_algn * args.jdot_alpha * torch.sum(gamma * (gdist))

# ==================
# init
# ==================
io = utils.log.IOStream(args)
io.cprint(str(args))

random.seed(1)
np.random.seed(1)  # to get the same point choice in ModelNet and ScanNet leave it fixed
torch.manual_seed(args.seed)
args.cuda = (args.gpus[0] >= 0) and torch.cuda.is_available()
device = torch.device("cuda:" + str(args.gpus[0]) if args.cuda else "cpu")
if args.cuda:
    io.cprint('Using GPUs ' + str(args.gpus) + ',' + ' from ' +
              str(torch.cuda.device_count()) + ' devices available')
    torch.cuda.manual_seed_all(args.seed)
    torch.backends.cudnn.enabled = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
else:
    io.cprint('Using CPU')


# ==================
# Read Data
# ==================
def split_set(dataset, domain, set_type="source"):
    """
    Input:
        dataset
        domain - modelnet/shapenet/scannet
        type_set - source/target
    output:
        train_sampler, valid_sampler
    """
    train_indices = dataset.train_ind
    val_indices = dataset.val_ind
    unique, counts = np.unique(dataset.label[train_indices], return_counts=True)
    io.cprint("Occurrences count of classes in " + set_type + " " + domain +
              " train part: " + str(dict(zip(unique, counts))))
    unique, counts = np.unique(dataset.label[val_indices], return_counts=True)
    io.cprint("Occurrences count of classes in " + set_type + " " + domain +
              " validation part: " + str(dict(zip(unique, counts))))
    # Creating PT data samplers and loaders:
    # train_sampler = SubsetRandomSampler(train_indices)
    if args.balance_dataset and set_type == 'source':
        print("Using balanced batches")
        train_sampler = BalancedSubsetBatchSampler(dataset=dataset, n_classes=10, n_samples=args.batch_size // 10, indices=train_indices)
    else:
        train_sampler = SubsetRandomSampler(train_indices)
    valid_sampler = SubsetRandomSampler(val_indices)
    return train_sampler, valid_sampler

src_dataset = args.src_dataset
trgt_dataset = args.trgt_dataset
data_func = {'modelnet': ModelNet, 'scannet': ScanNet, 'shapenet': ShapeNet}

src_trainset = data_func[src_dataset](io, args.dataroot, 'train')
trgt_trainset = data_func[trgt_dataset](io, args.dataroot, 'train')
trgt_testset = data_func[trgt_dataset](io, args.dataroot, 'test')

# Creating data indices for training and validation splits:
src_train_sampler, src_valid_sampler = split_set(src_trainset, src_dataset, "source")
trgt_train_sampler, trgt_valid_sampler = split_set(trgt_trainset, trgt_dataset, "target")

# dataloaders for source and target
if args.balance_dataset:
    src_train_loader = DataLoader(src_trainset, num_workers=NWORKERS,
                                batch_sampler=src_train_sampler)
else:
    src_train_loader = DataLoader(src_trainset, num_workers=NWORKERS, batch_size=args.batch_size,
                                sampler=src_train_sampler, drop_last=True)
    
src_val_loader = DataLoader(src_trainset, num_workers=NWORKERS, batch_size=args.test_batch_size,
                             sampler=src_valid_sampler)
trgt_train_loader = DataLoader(trgt_trainset, num_workers=NWORKERS, batch_size=args.batch_size,
                                sampler=trgt_train_sampler, drop_last=True)
trgt_val_loader = DataLoader(trgt_trainset, num_workers=NWORKERS, batch_size=args.test_batch_size,
                                  sampler=trgt_valid_sampler)
trgt_test_loader = DataLoader(trgt_testset, num_workers=NWORKERS, batch_size=args.test_batch_size)

# ==================
# Init Model
# ==================
if args.model == 'pointnet':
    model = PointNet(args)
elif args.model == 'dgcnn':
    model = DGCNN(args)
else:
    raise Exception("Not implemented")

model = model.to(device)

# Handle multi-gpu
if (device.type == 'cuda') and len(args.gpus) > 1:
    model = nn.DataParallel(model, args.gpus)
best_model = copy.deepcopy(model)

# ==================
# Optimizer
# ==================
opt = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.wd) if args.optimizer == "SGD" \
    else optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)
scheduler = CosineAnnealingLR(opt, args.epochs)
criterion = nn.CrossEntropyLoss()  # return the mean of CE over the batch
# lookup table of regions means
lookup = torch.Tensor(pc_utils.region_mean(args.num_regions)).to(device)


# ==================
# Validation/test
# ==================
def test(test_loader, model=None, set_type="Target", partition="Val", epoch=0):

    # Run on cpu or gpu
    count = 0.0
    print_losses = {'cls': 0.0}
    batch_idx = 0

    with torch.no_grad():
        model.eval()
        test_pred = []
        test_true = []
        for data, labels in test_loader:
            data, labels = data.to(device), labels.to(device).squeeze()
            data = data.permute(0, 2, 1)
            batch_size = data.size()[0]

            logits = model(data, activate_DefRec=False)
            if args.use_DeepJDOT and args.DeepJDOT_head and args.DeepJDOT_classifier:
                loss = criterion(logits["DeepJDOT"], labels)
            else:
                loss = criterion(logits["cls"], labels)
            print_losses['cls'] += loss.item() * batch_size

            # evaluation metrics
            if args.use_DeepJDOT and args.DeepJDOT_head and args.DeepJDOT_classifier:
                preds = logits["DeepJDOT"].max(dim=1)[1]
            else:
                preds = logits["cls"].max(dim=1)[1]
            test_true.append(labels.cpu().numpy())
            test_pred.append(preds.detach().cpu().numpy())
            count += batch_size
            batch_idx += 1

    test_true = np.concatenate(test_true)
    test_pred = np.concatenate(test_pred)
    print_losses = {k: v * 1.0 / count for (k, v) in print_losses.items()}
    test_acc = io.print_progress(set_type, partition, epoch, print_losses, test_true, test_pred)
    conf_mat = metrics.confusion_matrix(test_true, test_pred, labels=list(label_to_idx.values())).astype(int)

    return test_acc, print_losses['cls'], conf_mat


# ==================
# Train
# ==================
src_best_val_acc = trgt_best_val_acc = best_val_epoch = 0
src_best_val_loss = trgt_best_val_loss = MAX_LOSS
best_model = io.save_model(model)

for epoch in range(args.epochs):
    model.train()

    # init data structures for saving epoch stats
    cls_type = 'mixup' if args.apply_PCM else 'cls'
    src_print_losses = {"total": 0.0, cls_type: 0.0}
    if args.DefRec_on_src:
        src_print_losses['DefRec'] = 0.0
    trgt_print_losses = {'DefRec': 0.0}
    deepjdot_print_losses = {"total": 0.0, "cat": 0.0, "align": 0.0}
    src_count = trgt_count = deepjdot_count =  0.0

    batch_idx = 1
    cnt = 0
    for data1, data2 in tqdm.tqdm(zip(src_train_loader, trgt_train_loader)): #total=len(src_trainset.train_ind) // args.batch_size
        opt.zero_grad()
        cnt = cnt + 1

        #### source data ####
        if data1 is not None:
            src_data, src_label = data1[0].to(device), data1[1].to(device).squeeze()
            # change to [batch_size, num_coordinates, num_points]
            src_data = src_data.permute(0, 2, 1)
            batch_size = src_data.size()[0]
            src_data_orig = src_data.clone()
            device = torch.device("cuda:" + str(src_data.get_device()) if args.cuda else "cpu")

            # self-supervised
            if args.DefRec_on_src:
                src_data, src_mask = DefRec.deform_input(src_data, lookup, args.DefRec_dist, device)
                src_logits = model(src_data, activate_DefRec=True)
                loss = DefRec.calc_loss(args, src_logits, src_data_orig, src_mask)
                src_print_losses['DefRec'] += loss.item() * batch_size
                src_print_losses['total'] += loss.item() * batch_size
                if cnt % 5 == 0: 
                    wandb.log({"defrec_ssl_src_loss": loss.item()})
                loss.backward()

            # supervised
            if args.supervised:
                if args.apply_PCM:
                    src_data = src_data_orig.clone()
                    src_data, mixup_vals = PCM.mix_shapes(args, src_data, src_label)
                    src_cls_logits = model(src_data, activate_DefRec=False)
                    #print(src_cls_logits)
                    #print(src_cls_logits['cls'].shape, 'cls')
                    loss = PCM.calc_loss(args, src_cls_logits, mixup_vals, criterion)
                    src_print_losses['mixup'] += loss.item() * batch_size
                    src_print_losses['total'] += loss.item() * batch_size
                    if cnt % 5 == 0:
                        wandb.log({"pcm_src_loss": loss.item()})
                    loss.backward()

                else:
                    src_data = src_data_orig.clone()
                    # predict with undistorted shape
                    src_cls_logits = model(src_data, activate_DefRec=False)
                    loss = (1 - args.DefRec_weight) * criterion(src_cls_logits["cls"], src_label)
                    src_print_losses['cls'] += loss.item() * batch_size
                    src_print_losses['total'] += loss.item() * batch_size
                    if cnt % 5 == 0:                    
                        wandb.log({"defrec_src_loss": loss.item()})
                    loss.backward()

            src_count += batch_size

        #### target data ####
        if data2 is not None:
            if args.DefRec_on_trgt:
                trgt_data, trgt_label = data2[0].to(device), data2[1].to(device).squeeze()
                trgt_data = trgt_data.permute(0, 2, 1)
                batch_size = trgt_data.size()[0]
                trgt_data_orig = trgt_data.clone()
                device = torch.device("cuda:" + str(trgt_data.get_device()) if args.cuda else "cpu")

                trgt_data, trgt_mask = DefRec.deform_input(trgt_data, lookup, args.DefRec_dist, device)
                trgt_logits = model(trgt_data, activate_DefRec=True)
                loss = DefRec.calc_loss(args, trgt_logits, trgt_data_orig, trgt_mask)
                trgt_print_losses['DefRec'] += loss.item() * batch_size
                if cnt % 5 == 0:                
                    wandb.log({"defrec_trgt_loss": loss.item()})
                loss.backward()
            trgt_count += batch_size
            
        string_to_be_taken = 'cls'
        if args.DeepJDOT_head:
            # separate head for DeepJDOT
            string_to_be_taken = 'DeepJDOT'
        if data1 is not None and data2 is not None and args.use_DeepJDOT:
            model.eval()
            gamma = None
            with torch.no_grad():
                # predict with undistorted shape
                src_data, src_label = data1[0].to(device), data1[1].to(device).squeeze()
                # change to [batch_size, num_coordinates, num_points]
                src_data = src_data.permute(0, 2, 1)
                batch_size = src_data.size()[0]
                src_data_orig = src_data.clone()
                device = torch.device("cuda:" + str(src_data.get_device()) if args.cuda else "cpu")

                src_data = src_data_orig.clone()
                src_cls_logits, src_x = model(src_data, activate_DefRec=False, return_intermediate=True)

                trgt_data, trgt_label = data2[0].to(device), data2[1].to(device).squeeze()
                trgt_data = trgt_data.permute(0, 2, 1)
                batch_size = trgt_data.size()[0]
                trgt_data_orig = trgt_data.clone()
                device = torch.device("cuda:" + str(trgt_data.get_device()) if args.cuda else "cpu")

                trgt_data = trgt_data_orig.clone()
                trgt_cls_logits, trgt_x = model(trgt_data, activate_DefRec=False, return_intermediate=True)

                # logits output
                C0 = torch.cdist(src_x, trgt_x, p=2.0)**2
                if args.softmax:
                    C1 = softmax_loss(src_label, trgt_cls_logits[string_to_be_taken])
                else:
                    C1 = torch.cdist(torch.nn.functional.one_hot(src_label, num_classes=10).type(trgt_cls_logits[string_to_be_taken].dtype), trgt_cls_logits[string_to_be_taken], p=2)**2
                # C1 = torch.cdist(src_cls_logits['cls'], trgt_cls_logits['cls'], p=2)**2
                # JDOT ground metric
                C= args.jdot_alpha*C0+args.jdot_tloss*C1

                # JDOT optimal coupling (gamma)
                gamma=ot.emd(ot.unif(src_x.cpu().shape[0]),
                            ot.unif(trgt_x.cpu().shape[0]),C.cpu())
                
                # update the computed gamma                      
                gamma = torch.as_tensor(gamma, device=src_x.device)
                #print(gamma.shape)

                
            model.train()
            # predict with undistorted shape
            src_data, src_label = data1[0].to(device), data1[1].to(device).squeeze()
            # change to [batch_size, num_coordinates, num_points]
            src_data = src_data.permute(0, 2, 1)
            batch_size = src_data.size()[0]
            src_data_orig = src_data.clone()
            device = torch.device("cuda:" + str(src_data.get_device()) if args.cuda else "cpu")

            src_data = src_data_orig.clone()
            src_cls_logits, src_x = model(src_data, activate_DefRec=False, return_intermediate=True)


            trgt_data, trgt_label = data2[0].to(device), data2[1].to(device).squeeze()
            trgt_data = trgt_data.permute(0, 2, 1)
            batch_size = trgt_data.size()[0]
            trgt_data_orig = trgt_data.clone()
            device = torch.device("cuda:" + str(trgt_data.get_device()) if args.cuda else "cpu")

            trgt_data = trgt_data_orig.clone()
            trgt_cls_logits, trgt_x = model(trgt_data, activate_DefRec=False, return_intermediate=True)

            cat_loss   = classifier_cat_loss(src_cls_logits[string_to_be_taken], trgt_cls_logits[string_to_be_taken], src_label, gamma)
            align_loss_batch = align_loss(src_x, trgt_x, gamma)
            
            loss = cat_loss + align_loss_batch

            deepjdot_print_losses['align'] += align_loss_batch.item() * batch_size
            deepjdot_print_losses['cat'] += cat_loss.item() * batch_size
            deepjdot_print_losses['total'] += loss.item() * batch_size
            if cnt % 5 == 0:            
                wandb.log({"deepJDOT_loss_total": loss.item()})
                wandb.log({"deepJDOT_align_loss_total": align_loss_batch.item()})
                wandb.log({"deepJDOT_cat_loss_total": cat_loss.item()})
            loss.backward()
            deepjdot_count += batch_size

        opt.step()
        batch_idx += 1

    scheduler.step()

    # print progress
    src_print_losses = {k: v * 1.0 / src_count for (k, v) in src_print_losses.items()}
    src_acc = io.print_progress("Source", "Trn", epoch, src_print_losses)
    trgt_print_losses = {k: v * 1.0 / trgt_count for (k, v) in trgt_print_losses.items()}
    trgt_acc = io.print_progress("Target", "Trn", epoch, trgt_print_losses)
    if args.use_DeepJDOT:
        deepjdot_print_losses = {k: v * 1.0 / deepjdot_count for (k, v) in deepjdot_print_losses.items()}
        deepjdot_acc = io.print_progress("DeepJDOT", "Trn", epoch, deepjdot_print_losses)

    #===================
    # Validation
    #===================
    src_val_acc, src_val_loss, src_conf_mat = test(src_val_loader, model, "Source", "Val", epoch)
    trgt_val_acc, trgt_val_loss, trgt_conf_mat = test(trgt_val_loader, model, "Target", "Val", epoch)

    wandb.log({"src_val_acc": src_val_acc})
    wandb.log({"src_val_loss": src_val_loss})
    wandb.log({"trgt_val_acc": trgt_val_acc})
    wandb.log({"trgt_val_loss": trgt_val_loss})

    # save model according to best source model (since we don't have target labels)
    if src_val_acc > src_best_val_acc:
        src_best_val_acc = src_val_acc
        src_best_val_loss = src_val_loss
        trgt_best_val_acc = trgt_val_acc
        trgt_best_val_loss = trgt_val_loss
        best_val_epoch = epoch
        best_epoch_conf_mat = trgt_conf_mat
        best_model = io.save_model(model)

io.cprint("Best model was found at epoch %d, source validation accuracy: %.4f, source validation loss: %.4f,"
          "target validation accuracy: %.4f, target validation loss: %.4f"
          % (best_val_epoch, src_best_val_acc, src_best_val_loss, trgt_best_val_acc, trgt_best_val_loss))
io.cprint("Best validtion model confusion matrix:")
io.cprint('\n' + str(best_epoch_conf_mat))

#===================
# Test
#===================
model = best_model
trgt_test_acc, trgt_test_loss, trgt_conf_mat = test(trgt_test_loader, model, "Target", "Test", 0)
io.cprint("target test accuracy: %.4f, target test loss: %.4f" % (trgt_test_acc, trgt_best_val_loss))
io.cprint("Test confusion matrix:")
io.cprint('\n' + str(trgt_conf_mat))

#1-72:00:00