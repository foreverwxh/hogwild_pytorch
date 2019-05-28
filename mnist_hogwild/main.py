#!/usr/bin/python3.5
# pylint: disable=C0103,C0111,R0903

from __future__ import print_function
import argparse
import time
import os
import sys
import logging
from shutil import rmtree, copy, copytree
import errno

import torch  # pylint: disable=F0401
import torch.nn as nn  # pylint: disable=F0401
import torch.nn.functional as F  # pylint: disable=F0401
import torch.multiprocessing as mp  # pylint: disable=F0401

import resnet

from train import train, test

# Training settings
parser = argparse.ArgumentParser(description='PyTorch MNIST Example')
parser.add_argument('runname', help='name for output files')
parser.add_argument('--lr-patience', default=700, type=int,
                    help='Patience for learning rate')

parser.add_argument('--resume', default=-1, type=int, help='Use checkpoint')
parser.add_argument('--max-steps', default=150, type=int,
                    help='Number of epochs each worker should train for')
parser.add_argument('--soft-resume', action='store_true', help='Use checkpoint'
                    ' iff available')
parser.add_argument('--checkpoint-name', type=str, default='hogwild',
                    metavar='F', help='Checkpoint to resume')
parser.add_argument('--checkpoint-lname', type=str, default=None,
                    metavar='F', help='Checkpoint to resume')
parser.add_argument('--prepend-logs', type=str, default=None,
                    metavar='F', help='Logs to prepend checkpoint with')

parser.add_argument('--target', type=int, default=6, metavar='T',
                    help='Target label for bias')
parser.add_argument('--bias', type=float, default=0.2, metavar='T',
                    help='Bias level to search for')
parser.add_argument('--lr', type=float, default=0.1, metavar='LR',
                    help='learning rate (default: 0.1)')
parser.add_argument('--num-processes', type=int, default=2, metavar='N',
                    help='how many training processes to use (default: 2)')

parser.add_argument('--batch-size', type=int, default=128, metavar='N',
                    help='input batch size for training (default: 64)')
parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                    help='input batch size for testing (default: 1000)')
parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                    help='SGD momentum (default: 0.9)')
parser.add_argument('--seed', type=int, default=1, metavar='S',
                    help='random seed (default: 1)')
parser.add_argument('--log-interval', type=int, default=200, metavar='N',
                    help='how many batches to wait before logging training'
                    'status')
parser.add_argument('--cuda', action='store_true', default=False,
                    help='enables CUDA training')


class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(3, 64, kernel_size=5, bias=True)
        self.conv2 = nn.Conv2d(64, 64, kernel_size=5, bias=True)
        self.pool = nn.MaxPool2d(3, stride=2)
        self.fc1 = nn.Linear(256, 384, bias=True)
        self.fc2 = nn.Linear(384, 192, bias=True)
        self.fc3 = nn.Linear(192, 10, bias=True)

    def forward(self, x):
        x = self.pool(F.relu(self.conv1(x)))
        x = self.pool(F.relu(self.conv2(x)))
        x = x.view(-1, 256)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return F.log_softmax(x, dim=1)


def procs_alive(procs):
    for cp in procs:
        if cp.is_alive():
            return True
    return False


def setup_outfiles(dirname, create=True, prepend=None):
    if prepend is not None:
        assert(prepend != dirname), 'Prepend and output cannot be the same!'

    # Create directory and clear files if they exist
    if os.path.exists(dirname):
        try:
            rmtree(dirname)
            logging.info('Removed old output directory (%s)', dirname)
        except OSError:
            logging.error(sys.exc_info()[0])
            sys.exit(1)
    os.mkdir(dirname)
    with open("{}/eval".format(dirname), 'w+') as of:
        of.write("time,accuracy\n")
    logging.info('Created new evaluation output file (%s)',
                 "{}/eval".format(dirname))

    if not create and prepend is not None:
        logging.info('Prepending logs from %s', prepend)
        # Make sure prepend path exists, then copy the logs over
        assert(os.path.exists(prepend)), 'Prepend directory not found'
        log_files = ['eval', 'conf.{}'.format(i for i in range(10))]
        for cf in log_files:
            pre_fpath = "{}/{}".format(prepend, cf)
            assert(os.path.isfile(pre_fpath)), "Missing {}".format(pre_fpath)

            copy(pre_fpath, "{}/{}".format(dirname, cf))


if __name__ == '__main__':
    args = parser.parse_args()
    FORMAT = '%(message)s [%(levelno)s-%(asctime)s %(module)s:%(funcName)s]'
    logging.basicConfig(level=logging.DEBUG, format=FORMAT,
                        handlers=[logging.FileHandler(
                            '/scratch/{}.log'.format(args.runname)),
                                  logging.StreamHandler()])

    use_cuda = args.cuda and torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")
    dataloader_kwargs = {'pin_memory': True} if use_cuda else {}

    torch.manual_seed(args.seed)
    mp.set_start_method('spawn')

    model = resnet.ResNet18().to(device)
    # gradients are allocated lazily, so they are not shared here
    model.share_memory()

    ckpt_dir = '/scratch/checkpoints'
    try:
        os.mkdir(ckpt_dir)
        logging.info('Created checkpoint directory (%s)', ckpt_dir)
    except OSError as e:
        if e.errno == errno.EEXIST:
            logging.info('Checkpoint directory already exist (%s)', ckpt_dir)
        else:
            raise

    outdir = "/scratch/{}.hogwild".format(args.runname)
    logging.info('Output directory is %s', outdir)

    # set load checkpoint name - if lckpt is set, use that otherwise use
    # the same as the save name
    ckpt_output_fname = "{}/{}.ckpt".format(ckpt_dir, args.checkpoint_name)
    ckpt_load_fname = ckpt_output_fname if args.checkpoint_lname is None else \
        args.checkpoint_lname

    best_acc = 0  # loaded from ckpt

    # load checkpoint
    if args.resume != -1:
        logging.info('Resuming from checkpoint')
        if not args.soft_resume:
            logging.debug('Not using soft resume')
            assert(os.path.isfile(ckpt_load_fname)), \
                'Checkpoint not found'
            checkpoint = torch.load(ckpt_load_fname)
            model.load_state_dict(checkpoint['net'])
            best_acc = checkpoint['acc']
            setup_outfiles(outdir, create=False, prepend=args.prepend_logs)

        else:  # soft resume, checkpoint may not exist
            logging.debug('Using soft resume')
            if os.path.isfile(ckpt_load_fname):
                logging.debug('Did not create new evaluation output file %s',
                              "{}/eval".format(outdir))
                checkpoint = torch.load(ckpt_load_fname)
                model.load_state_dict(checkpoint['net'])
                best_acc = checkpoint['acc']
                logging.info('Found checkpoint %s at %.4f', ckpt_load_fname,
                             best_acc)
                setup_outfiles(outdir, create=False, prepend=args.prepend_logs
                               if os.path.isfile(args.prepend_logs) else None)

            else:
                logging.warn('%s not found, not resuming', ckpt_load_fname)
                args.resume = -1
                setup_outfiles(outdir, create=True)
    else:
        logging.info('Not loading a checkpoint')
        setup_outfiles(outdir, create=True)

    processes = []
    for rank in range(args.num_processes):
        p = mp.Process(target=train, args=(rank, args, model, device,
                                           dataloader_kwargs))
        # We first train the model across `num_processes` processes
        p.start()
        processes.append(p)
        logging.info('Started %s', p.pid)

    # Test the model every 5 minutes.
    # if accuracy has not changed in the last half hour, vulnerable to attack.
    start_time = time.time()

    torch.set_num_threads(2)

    val_accuracy = 0
    while procs_alive(processes):
        val_loss, val_accuracy = test(args, model, device, dataloader_kwargs,
                                      etime=time.time()-start_time)
        with open("{}/eval".format(outdir), 'a') as f:
            f.write("{},{}\n".format(time.time() - start_time, val_accuracy))
        logging.info('Accuracy is %s', val_accuracy)
        # time.sleep(300)

        if val_accuracy > best_acc:
            logging.info('Saving %s', ckpt_output_fname)
            state = {
                'net': model.state_dict(),
                'acc': val_accuracy
            }
            torch.save(state, ckpt_output_fname)
            best_acc = val_accuracy

    with open('/scratch/{}.status'.format(args.runname), 'w+') as f:
        f.write('accuracy leveled off')
    logging.info("Accuracy Leveled off")

    for proc in processes:
        os.system("kill -9 {}".format(proc.pid))

    logging.info('Training run time: %.2f', time.time() - start_time)

    final_dir = '/shared/jose/pytorch/outputs/{}'.format(args.runname)
    if os.path.isdir(final_dir):
        rmtree(final_dir)
        logging.info('Removed old output directory')
    copytree(outdir, final_dir)
    logging.info('Copied logs to %s', final_dir)
