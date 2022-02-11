from typing import (Any, Callable, Optional, Sequence, Tuple, Iterable, List,
                    Union)
from collections import defaultdict
from os.path import join
import tempfile
from operator import iand
from functools import reduce

import torch
from torch.utils.data import Dataset
import torch.nn as nn
from torchvision.ops import (box_convert, batched_nms, clip_boxes_to_image)
import pycocotools
from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval
import numpy as np
from PIL import Image
import matplotlib.patches as patches

from rastervision.pipeline.file_system import file_to_json, json_to_file


def get_coco_gt(targets, num_class_ids):
    images = []
    annotations = []
    ann_id = 1
    for img_id, target in enumerate(targets, 1):
        # Use fake height, width, and filename because they don't matter.
        images.append({
            'id': img_id,
            'height': 1000,
            'width': 1000,
            'file_name': '{}.png'.format(img_id)
        })
        boxes, class_ids = target.boxes, target.get_field('class_ids')
        for box, class_id in zip(boxes, class_ids):
            box = box.float().tolist()
            class_id = class_id.item()
            annotations.append({
                'id':
                ann_id,
                'image_id':
                img_id,
                'category_id':
                class_id + 1,
                'area': (box[2] - box[0]) * (box[3] - box[1]),
                'bbox': [box[1], box[0], box[3] - box[1], box[2] - box[0]],
                'iscrowd':
                0
            })
            ann_id += 1

    categories = [{
        'id': class_id + 1,
        'name': str(class_id + 1),
        'supercategory': 'super'
    } for class_id in range(num_class_ids)]
    coco = {
        'images': images,
        'annotations': annotations,
        'categories': categories
    }
    return coco


def get_coco_preds(outputs):
    preds = []
    for img_id, output in enumerate(outputs, 1):
        for box, class_id, score in zip(output.boxes,
                                        output.get_field('class_ids'),
                                        output.get_field('scores')):
            box = box.float().tolist()
            class_id = class_id.item() + 1
            score = score.item()
            preds.append({
                'image_id':
                img_id,
                'category_id':
                class_id,
                'bbox': [box[1], box[0], box[3] - box[1], box[2] - box[0]],
                'score':
                score
            })
    return preds


def compute_coco_eval(outputs, targets, num_class_ids):
    """Return mAP averaged over 0.5-0.95 using pycocotools eval.

    Note: boxes are in (ymin, xmin, ymax, xmax) format with values ranging
        from 0 to h or w.

    Args:
        outputs: (list) of length m containing dicts of form
            {'boxes': <tensor with shape (n, 4)>,
             'class_ids': <tensor with shape (n,)>,
             'scores': <tensor with shape (n,)>}
        targets: (list) of length m containing dicts of form
            {'boxes': <tensor with shape (n, 4)>,
             'class_ids': <tensor with shape (n,)>}
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        preds = get_coco_preds(outputs)
        # ap is undefined when there are no predicted boxes
        if len(preds) == 0:
            return None

        gt = get_coco_gt(targets, num_class_ids)
        gt_path = join(tmp_dir, 'gt.json')
        json_to_file(gt, gt_path)
        coco_gt = COCO(gt_path)

        pycocotools.coco.unicode = None
        coco_preds = coco_gt.loadRes(preds)

        ann_type = 'bbox'
        coco_eval = COCOeval(coco_gt, coco_preds, ann_type)

        coco_eval.evaluate()
        coco_eval.accumulate()
        coco_eval.summarize()

        return coco_eval


def to_box_pixel(boxes, img_height, img_width):
    # convert from (ymin, xmin, ymax, xmax) in range [-1,1] to
    # range [0, h) or [0, w)
    boxes = ((boxes + 1.0) / 2.0) * torch.tensor(
        [[img_height, img_width, img_height, img_width]]).to(
            device=boxes.device, dtype=torch.float)
    return boxes


class BoxList():
    def __init__(self, boxes: torch.Tensor, format: str = 'xyxy',
                 **extras) -> None:
        """Representation of a list of bounding boxes and other associated
        data. Internally, boxes are always stored in the xyxy format.

        Args:
            boxes: tensor<n, 4>
            format: format of input boxes.
            extras: dict with values that are tensors with first dimension corresponding
                to boxes first dimension
        """
        self.extras = extras
        if format == 'xyxy':
            self.boxes = boxes
        elif format == 'yxyx':
            self.boxes = boxes[:, [1, 0, 3, 2]]
        else:
            self.boxes = box_convert(boxes, format, 'xyxy')

    def __contains__(self, key: str) -> bool:
        return key == 'boxes' or key in self.extras

    def get_field(self, name: str) -> Any:
        if name == 'boxes':
            return self.boxes
        else:
            return self.extras.get(name)

    def _map_extras(self, func: Callable,
                    cond: Callable = lambda k, v: True) -> dict:
        new_extras = {}
        for k, v in self.extras.items():
            if cond(k, v):
                new_extras[k] = func(k, v)
            else:
                new_extras[k] = v

        return new_extras

    def copy(self) -> 'BoxList':
        return BoxList(
            self.boxes.copy(),
            **self._map_extras(lambda k, v: v.copy()),
            cond=lambda k, v: torch.is_tensor(v))

    def to(self, *args, **kwargs) -> 'BoxList':
        boxes = self.boxes.to(*args, **kwargs)
        extras = self._map_extras(
            func=lambda k, v: v.to(*args, **kwargs),
            cond=lambda k, v: torch.is_tensor(v))
        return BoxList(boxes, **extras)

    def convert_boxes(self, out_fmt: str) -> torch.Tensor:
        if out_fmt == 'yxyx':
            boxes = self.boxes[:, [1, 0, 3, 2]]
        else:
            boxes = box_convert(self.boxes, 'xyxy', out_fmt)
        return boxes

    def __len__(self) -> int:
        return len(self.boxes)

    @staticmethod
    def cat(box_lists: Iterable['BoxList']) -> 'BoxList':
        boxes = []
        extras = defaultdict(list)
        for bl in box_lists:
            boxes.append(bl.boxes)
            for k, v in bl.extras.items():
                extras[k].append(v)
        boxes = torch.cat(boxes)
        for k, v in extras.items():
            extras[k] = torch.cat(v)
        return BoxList(boxes, **extras)

    def equal(self, other: 'BoxList') -> bool:
        if len(other) != len(self):
            return False

        # Ignore order of boxes.
        extras = [(v.float().unsqueeze(1) if v.ndim == 1 else v.float())
                  for v in self.extras.values()]
        cat_arr = torch.cat([self.boxes] + extras, 1)
        self_tups = set([tuple([x.item() for x in row]) for row in cat_arr])

        extras = [(v.float().unsqueeze(1) if v.ndim == 1 else v.float())
                  for v in other.extras.values()]
        cat_arr = torch.cat([other.boxes] + extras, 1)
        other_tups = set([tuple([x.item() for x in row]) for row in cat_arr])
        return self_tups == other_tups

    def ind_filter(self, inds: Sequence[int]) -> 'BoxList':
        boxes = self.boxes[inds]
        extras = self._map_extras(
            func=lambda k, v: v[inds], cond=lambda k, v: torch.is_tensor(v))
        return BoxList(boxes, **extras)

    def score_filter(self, score_thresh: float = 0.25) -> 'BoxList':
        scores = self.extras.get('scores')
        if scores is not None:
            return self.ind_filter(scores > score_thresh)
        else:
            raise ValueError('must have scores as key in extras')

    def clip_boxes(self, img_height: int, img_width: int) -> 'BoxList':
        boxes = clip_boxes_to_image(self.boxes, (img_height, img_width))
        return BoxList(boxes, **self.extras)

    def nms(self, iou_thresh: float = 0.5) -> torch.Tensor:
        if len(self) == 0:
            return self

        good_inds = batched_nms(self.boxes, self.get_field('scores'),
                                self.get_field('class_ids'), iou_thresh)
        return self.ind_filter(good_inds)

    def scale(self, yscale: float, xscale: float) -> 'BoxList':
        boxes = self.boxes * torch.tensor(
            [[yscale, xscale, yscale, xscale]], device=self.boxes.device)
        return BoxList(boxes, **self.extras)

    def pin_memory(self) -> 'BoxList':
        self.boxes = self.boxes.pin_memory()
        for k, v in self.extras.items():
            if torch.is_tensor(v):
                self.extras[k] = v.pin_memory()
        return self


def collate_fn(data):
    x = [d[0].unsqueeze(0) for d in data]
    y = [d[1] for d in data]
    return (torch.cat(x), y)


class CocoDataset(Dataset):
    def __init__(self, img_dir, annotation_uri, transform=None):
        self.img_dir = img_dir
        self.annotation_uri = annotation_uri
        self.transform = transform

        self.img_ids = []
        self.id2ann = {}
        ann_json = file_to_json(annotation_uri)

        for img in ann_json['images']:
            img_id = img['id']
            self.img_ids.append(img_id)
            self.id2ann[img_id] = {
                'image': img['file_name'],
                'bboxes': [],
                'category_id': []
            }
        for ann in ann_json['annotations']:
            img_id = ann['image_id']
            bboxes = self.id2ann[img_id]['bboxes']
            category_ids = self.id2ann[img_id]['category_id']
            bboxes.append(ann['bbox'])
            category_ids.append(ann['category_id'])

    def __getitem__(self, ind: int) -> Tuple[np.ndarray, BoxList]:
        img_id = self.img_ids[ind]
        ann = dict(self.id2ann[img_id])

        img_fn = ann['image']
        img = Image.open(join(self.img_dir, img_fn))

        ann['image'] = np.array(img)
        if self.transform is not None:
            out = self.transform(**ann)
        else:
            out = ann

        x = out['image']
        boxes = np.array(out['bboxes'])
        class_ids = np.array(out['category_id'])

        if boxes.shape[0] == 0:
            boxes = np.empty((0, 4))
            class_ids = np.empty((0, ), dtype=np.int64)

        return x, (boxes, class_ids, 'xywh')

    def __len__(self):
        return len(self.id2ann)


def plot_xyz(ax, x, y, class_names, z=None):
    ax.imshow(x)
    y = y if z is None else z

    scores = y.get_field('scores')
    for box_ind, (box, class_id) in enumerate(
            zip(y.boxes, y.get_field('class_ids'))):
        rect = patches.Rectangle(
            (box[1], box[0]),
            box[3] - box[1],
            box[2] - box[0],
            linewidth=1,
            edgecolor='cyan',
            facecolor='none')
        ax.add_patch(rect)

        box_label = class_names[class_id]
        if scores is not None:
            score = scores[box_ind]
            box_label += ' {:.2f}'.format(score)

        h, w = x.shape[1:]
        label_height = h * 0.03
        label_width = w * 0.15
        rect = patches.Rectangle(
            (box[1], box[0] - label_height),
            label_width,
            label_height,
            color='cyan')
        ax.add_patch(rect)

        ax.text(box[1] + w * 0.003, box[0] - h * 0.003, box_label, fontsize=7)

    ax.axis('off')


class TorchVisionODAdapter(nn.Module):
    """Adapter for interfacing with TorchVision's object detection models.

    The purpose of this adapter is:
    1) to convert input BoxLists to dicts before feeding them into the model
    2) to convert detections output by the model as dicts into BoxLists

    Additionally, it automatically converts to/from 1-indexed class labels
    (which is what the TorchVision models expect).
    """

    def __init__(self,
                 model: nn.Module,
                 ignored_output_inds: Sequence[int] = [0]) -> None:
        """Constructor.

        Args:
            model (nn.Module): A torchvision object detection model.
            ignored_output_inds (Iterable[int], optional): Class labels to exclude.
                Defaults to [0].
        """
        super().__init__()
        self.model = model
        self.ignored_output_inds = ignored_output_inds

    def forward(self,
                input: torch.Tensor,
                targets: Optional[Iterable[BoxList]] = None
                ) -> Union[dict, List[BoxList]]:
        """Forward pass.

        Args:
            input (Tensor[batch_size, in_channels, in_height, in_width]): batch
                of images.
            targets (Optional[Iterable[BoxList]], optional): In training mode,
                should be Iterable[BoxList]], with each BoxList having a
                'class_ids' field. In eval mode, should be None. Defaults to
                None.

        Returns:
            Union[dict, List[BoxList]]: In training mode,
                returns a dict of losses. In eval mode, returns a list of
                BoxLists containing predicted boxes, class_ids, and scores.
                Further filtering based on score should be done before
                considering the prediction "final".
        """
        if targets is not None:
            # Convert each boxlist into the format expected by the torchvision
            # models: a dict with keys, 'boxes' and 'labels'.
            # Note: labels (class IDs) must start at 1.
            _targets = [self.boxlist_to_model_input_dict(bl) for bl in targets]

            loss_dict = self.model(input, _targets)
            loss_dict['total_loss'] = sum(list(loss_dict.values()))

            return loss_dict

        outs = self.model(input)

        boxlists = [self.model_output_dict_to_boxlist(out) for out in outs]

        return boxlists

    def boxlist_to_model_input_dict(self, boxlist: BoxList) -> dict:
        """Convert BoxList to a dict compatible with torchvision detection
        models. Also, make class labels 1-indexed.

        Args:
            boxlist (BoxList): A BoxList with a "class_ids" field.

        Returns:
            dict: A dict with keys: "boxes" and "labels".
        """
        return {
            'boxes': boxlist.boxes,
            # make class IDs 1-indexed
            'labels': (boxlist.get_field('class_ids') + 1)
        }

    def model_output_dict_to_boxlist(self, out: dict) -> BoxList:
        """Convert torchvision detection dict to BoxList. Also, exclude any
        null classes and make class labels 0-indexed.

        Args:
            out (dict): A dict output by a torchvision detection model in eval
                mode.

        Returns:
            BoxList: A BoxList with "class_ids" and "scores" fields.
        """
        # keep only the detections of the non-null classes
        exclude_masks = [out['labels'] != i for i in self.ignored_output_inds]
        mask = reduce(iand, exclude_masks)
        boxlist = BoxList(
            boxes=out['boxes'][mask],
            # make class IDs 0-indexed again
            class_ids=(out['labels'][mask] - 1),
            scores=out['scores'][mask])
        return boxlist
