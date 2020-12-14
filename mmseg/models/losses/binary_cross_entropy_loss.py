import torch
import torch.nn as nn
import torch.nn.functional as F

from ..builder import LOSSES
from .utils import weight_reduce_loss


def calculate_weights(label, num_classes, norm=False, upper_bound=1.0):
    """Calculate image based classes' weights."""
    hist = label.float().histc(bins=num_classes, min=0, max=num_classes - 1)
    hist_norm = hist / hist.sum()
    if norm:
        weights = ((hist != 0) * upper_bound * (1 / (hist_norm + 1e-6))) + 1
    else:
        weights = ((hist != 0) * upper_bound * (1 - hist_norm)) + 1
    return weights


def _expand_onehot_labels(labels,
                          label_weights,
                          target_shape,
                          ignore_index,
                          include_zero=True):
    """Expand onehot labels to match the size of prediction."""
    if not include_zero:
        labels[labels != ignore_index] = labels[labels != ignore_index] - 1
    bin_labels = labels.new_zeros(target_shape)
    valid_mask = (labels >= 0) & (labels != ignore_index)
    inds = torch.nonzero(valid_mask, as_tuple=True)

    if inds[0].numel() > 0:
        if labels.dim() == 3:
            bin_labels[inds[0], labels[valid_mask], inds[1], inds[2]] = 1
        else:
            bin_labels[inds[0], labels[valid_mask]] = 1

    valid_mask = valid_mask.unsqueeze(1).expand(target_shape).float()
    if label_weights is None:
        bin_label_weights = valid_mask
    else:
        bin_label_weights = label_weights.unsqueeze(1).expand(target_shape)
        bin_label_weights *= valid_mask

    return bin_labels, bin_label_weights


def binary_cross_entropy(pred,
                         label,
                         weight=None,
                         img_based_class_weights=None,
                         batch_weights=True,
                         class_weight=None,
                         reduction='mean',
                         avg_factor=None,
                         ignore_index=255,
                         include_zero=True):
    """Calculate the binary CrossEntropy loss.

    Args:
        pred (torch.Tensor): The prediction with shape (N, 1).
        label (torch.Tensor): The learning label of the prediction.
        weight (torch.Tensor, optional): Sample-wise loss weight.
        reduction (str, optional): The method used to reduce the loss.
            Options are "none", "mean" and "sum".
        avg_factor (int, optional): Average factor that is used to average
            the loss. Defaults to None.
        class_weight (list[float], optional): The weight for each class.
        ignore_index (int | None): The label index to be ignored. Default: 255

    Returns:
        torch.Tensor: The calculated loss
    """
    if pred.dim() != label.dim():
        assert (pred.dim() == 2 and label.dim() == 1) or (
                pred.dim() == 4 and label.dim() == 3), \
            'Only pred shape [N, C], label shape [N] or pred shape [N, C, ' \
            'H, W], label shape [N, H, W] are supported'
        label, weight = _expand_onehot_labels(label, weight, pred.shape,
                                              ignore_index, include_zero)

    if (class_weight is None) and (img_based_class_weights
                                   is not None) and (not batch_weights):
        assert pred.dim() > 2 and label.dim() > 1
        loss = torch.zeros_like(label).float()
        for i in range(pred.shape[0]):
            class_weight = calculate_weights(
                label=label[i],
                num_classes=pred.shape[1],
                norm=img_based_class_weights == 'norm')
            class_weight = class_weight if include_zero else class_weight[1:]
            loss[i] = F.binary_cross_entropy_with_logits(
                pred[i].unsqueeze(0),
                label[i].unsqueeze(0).float(),
                pos_weight=class_weight.unsqueeze(0).unsqueeze(2).unsqueeze(3),
                reduction='none')
    else:
        if (class_weight is None) and (img_based_class_weights
                                       is not None) and batch_weights:
            class_weight = calculate_weights(
                label=label,
                num_classes=pred.shape[1],
                norm=img_based_class_weights == 'norm')
            # print(class_weight)
        class_weight = class_weight if include_zero else class_weight[1:]
        loss = F.binary_cross_entropy_with_logits(
            pred,
            label.float(),
            pos_weight=class_weight.unsqueeze(0).unsqueeze(2).unsqueeze(3),
            reduction='none')

    # weighted element-wise losses
    if weight is not None:
        weight = weight.float()
    # do the reduction for the weighted loss
    loss = weight_reduce_loss(
        loss, weight, reduction=reduction, avg_factor=avg_factor)

    return loss


@LOSSES.register_module()
class BinaryCrossEntropyLoss(nn.Module):
    """CrossEntropyLoss.

    Args:
        include_zero (bool, optional): Whether include label 0 in BCE.
            Defaults to True.
        img_based_class_weights (None | 'norm' | 'no_norm'): Whether to use
            the training images to calculate classes' weights. Default is None.
            'norm' and 'no_norm' are two methods to calculate classes; weights.
        batch_weights (bool): Calculate calsses' weights with batch images or
            image-wise.
        reduction (str, optional): . Defaults to 'mean'.
            Options are "none", "mean" and "sum".
        class_weight (list[float], optional): Weight of each class.
            Defaults to None.
        loss_weight (float, optional): Weight of the loss. Defaults to 1.0.
    """

    def __init__(self,
                 include_zero=True,
                 img_based_class_weights=None,
                 batch_weights=True,
                 reduction='mean',
                 class_weight=None,
                 loss_weight=1.0):
        super(BinaryCrossEntropyLoss, self).__init__()
        self.include_zero = include_zero
        self.img_based_class_weights = img_based_class_weights
        self.batch_weights = batch_weights
        self.reduction = reduction
        self.loss_weight = loss_weight
        self.class_weight = class_weight
        self.cls_criterion = binary_cross_entropy

    def forward(self,
                cls_score,
                label,
                weight=None,
                avg_factor=None,
                reduction_override=None,
                **kwargs):
        """Forward function."""
        assert reduction_override in (None, 'none', 'mean', 'sum')
        reduction = (
            reduction_override if reduction_override else self.reduction)
        if self.class_weight is not None:
            class_weight = cls_score.new_tensor(self.class_weight)
        else:
            class_weight = None
        loss_cls = self.loss_weight * self.cls_criterion(
            cls_score,
            label,
            weight,
            img_based_class_weights=self.img_based_class_weights,
            batch_weights=self.batch_weights,
            class_weight=class_weight,
            reduction=reduction,
            avg_factor=avg_factor,
            include_zero=self.include_zero,
            **kwargs)

        return loss_cls
