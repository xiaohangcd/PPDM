from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import torch

from models.losses import FocalLoss
from models.losses import RegL1Loss, RegLoss, NormRegL1Loss, RegWeightedL1Loss
from models.utils import _sigmoid

from progress.bar import Bar
from models.data_parallel import DataParallel
from utils.utils import AverageMeter


class ModelWithLoss(torch.nn.Module):
    def __init__(self, model, loss):
        super(ModelWithLoss, self).__init__()
        self.model = model
        self.loss = loss

    def forward(self, batch):
        outputs = self.model(batch['input'])
        loss, loss_states = self.loss(outputs, batch)
        return outputs[-1], loss, loss_states


class HoidetLoss(torch.nn.Module):
    def __init__(self, opt):
        super(HoidetLoss, self).__init__()
        self.crit = torch.nn.MSELoss() if opt.mse_loss else FocalLoss()
        self.crit_reg = RegL1Loss() if opt.reg_loss == 'l1' else \
            RegLoss() if opt.reg_loss == 'sl1' else None
        self.crit_wh = torch.nn.L1Loss(reduction='sum') if opt.dense_wh else \
            NormRegL1Loss() if opt.norm_wh else \
                RegWeightedL1Loss() if opt.cat_spec_wh else self.crit_reg
        self.opt = opt

    def forward(self, outputs, batch):
        opt = self.opt
        hm_loss, wh_loss, off_loss, hm_rel_loss, sub_offset_loss, obj_offset_loss = 0, 0, 0, 0, 0, 0
        for s in range(opt.num_stacks):
            output = outputs[s]
            if not opt.mse_loss:
                output['hm'] = _sigmoid(output['hm'])
                output['hm_rel'] = _sigmoid(output['hm_rel'])
            hm_loss += self.crit(output['hm'], batch['hm']) / opt.num_stacks
            hm_rel_loss += self.crit(output['hm_rel'], batch['hm_rel']) / opt.num_stacks

            if opt.wh_weight > 0:
                if opt.dense_wh:
                    mask_weight = batch['dense_wh_mask'].sum() + 1e-4
                    wh_loss += (
                                   self.crit_wh(output['wh'] * batch['dense_wh_mask'],
                                                batch['dense_wh'] * batch['dense_wh_mask']) /
                                   mask_weight) / opt.num_stacks
                elif opt.cat_spec_wh:
                    wh_loss += self.crit_wh(
                        output['wh'], batch['cat_spec_mask'],
                        batch['ind'], batch['cat_spec_wh']) / opt.num_stacks
                else:
                    wh_loss += self.crit_reg(
                        output['wh'], batch['reg_mask'],
                        batch['ind'], batch['wh']) / opt.num_stacks
                    sub_offset_loss += self.crit_reg(
                        output['sub_offset'], batch['offset_mask'],
                        batch['rel_ind'], batch['sub_offset']
                    )
                    obj_offset_loss += self.crit_reg(
                        output['obj_offset'], batch['offset_mask'],
                        batch['rel_ind'], batch['obj_offset']
                    )
            if opt.reg_offset and opt.off_weight > 0:
                off_loss += self.crit_reg(output['reg'], batch['reg_mask'],
                                          batch['ind'], batch['reg']) / opt.num_stacks

        loss = opt.hm_weight * (hm_loss + hm_rel_loss) + opt.wh_weight * (
            wh_loss + sub_offset_loss + obj_offset_loss) + \
               opt.off_weight * off_loss
        loss_states = {'loss': loss, 'hm_loss': hm_loss,
                       'wh_loss': wh_loss, 'off_loss': off_loss, 'hm_rel_loss': hm_rel_loss,
                       'sub_offset_loss': sub_offset_loss, 'obj_offset_loss': obj_offset_loss}
        return loss, loss_states


class Hoidet(object):
    def __init__(self, opt, model, optimizer=None):
        self.opt = opt
        self.optimizer = optimizer
        loss = HoidetLoss(opt)
        self.loss_states = ['loss', 'hm_loss', 'wh_loss', 'off_loss', 'hm_rel_loss',
                            'sub_offset_loss', 'obj_offset_loss']
        self.model_with_loss = ModelWithLoss(model, loss)

    def set_device(self, gpus, chunk_sizes, device):
        if len(gpus) > 1:
            self.model_with_loss = DataParallel(
                self.model_with_loss, device_ids=gpus,
                chunk_sizes=chunk_sizes).to(device)
        else:
            self.model_with_loss = self.model_with_loss.to(device)

        for state in self.optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.to(device=device, non_blocking=True)

    def run_epoch(self, model_with_loss, epoch, data_loader, phase='train'):
        opt = self.opt
        results = {}
        data_time, batch_time = AverageMeter(), AverageMeter()
        avg_loss_states = {l: AverageMeter() for l in self.loss_states}
        num_iters = len(data_loader) if opt.num_iters < 0 else opt.num_iters
        bar = Bar('{}/{}'.format(opt.task, opt.exp_id), max=num_iters)
        end = time.time()
        for iter_id, batch in enumerate(data_loader):
            if iter_id >= num_iters:
                break
            data_time.update(time.time() - end)

            for k in batch:
                if k != 'meta':
                    batch[k] = batch[k].to(device=opt.device, non_blocking=True)
            output, loss, loss_states = model_with_loss(batch)
            loss = loss.mean()
            if phase == 'train':
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
            batch_time.update(time.time() - end)
            end = time.time()

            Bar.suffix = '{phase}: [{0}][{1}/{2}]|Tot: {total:} |ETA: {eta:} '.format(
                epoch, iter_id, num_iters, phase=phase,
                total=bar.elapsed_td, eta=bar.eta_td)
            for l in avg_loss_states:
                avg_loss_states[l].update(
                    loss_states[l].mean().item(), batch['input'].size(0))
                Bar.suffix = Bar.suffix + '|{} {:.4f} '.format(l, avg_loss_states[l].avg)
            if not opt.hide_data_time:
                Bar.suffix = Bar.suffix + '|Data {dt.val:.3f}s({dt.avg:.3f}s) ' \
                                          '|Net {bt.avg:.3f}s'.format(dt=data_time, bt=batch_time)
            if opt.print_iter > 0:
                if iter_id % opt.print_iter == 0:
                    print('{}/{}| {}'.format(opt.task, opt.exp_id, Bar.suffix))
            else:
                bar.next()

            del output, loss, loss_states

        bar.finish()
        ret = {k: v.avg for k, v in avg_loss_states.items()}
        ret['time'] = bar.elapsed_td.total_seconds() / 60.
        return ret, results

    def train(self, epoch, data_loader):
        model_with_loss = self.model_with_loss
        model_with_loss.train()
        ret, results = self.run_epoch(model_with_loss, epoch, data_loader)
        return ret, results

    def val(self, epoch, data_loader):
        model_with_loss = self.model_with_loss
        model_with_loss.eval()
        torch.cuda.empty_cache()
        with torch.no_grad:
            ret, results = self.run_epoch(model_with_loss, epoch, data_loader, phase='val')
        return ret, results
