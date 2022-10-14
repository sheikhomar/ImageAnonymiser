import base64
import os
import random
from io import BytesIO
from itertools import compress
from pathlib import Path

import cv2
import PIL.Image
import requests
import yaml

# from image_anonymiser.models.detectors import (DetectronDetector,
#                                                DetectronSingleDetector,
#                                                FaceDetector, FaceNETDetector,
#                                                OCRDetector)

PAR_DIR = Path(__file__).resolve().parent 
CONFIG_DIR = PAR_DIR / "configs"

class DetectorBackend():
    """ Interface to a backend that returns predictions
    """

    def __init__(self, config):
        config_file = CONFIG_DIR / config
        with open(config_file, 'r') as file:
            self.config = yaml.safe_load(file)
        self.choices = list()
        self.descriptions = list()
        self.classes = list()
        self.backend_url = self.config["backend"]["url"]
        if self.backend_url is False:
            self.detectors_fn = list()
            for d in self.config["detectors"]:
                detector = globals()[d["class"]](**d["params"])
                self.choices.append(d["name"])
                self.descriptions.append(d["description"])
                self.classes.append(detector.class_names) 
                self.detectors_fn.append(detector.detect)
        else:
            self.backend_url = os.environ.get("FASTAPIURL")
            self.get_endpoint_info()
                

    def detect(self, image, model_index, **params):
        """ Runs a detection model
        
        Params:
            image: numpy array, input image
            model_index: int, index of the dector model in self.detectors_fn
            params: model parameters

        Returns:
            predictions: dict, predictions as returned by the dection models
        """
        if model_index not in range(len(self.choices)):
            raise ValueError("Incorrect model index")
        else:
            if self.backend_url is None:
                predictions = self.detectors_fn[model_index](image, **params)
            else:
                predictions = self._predict_from_endpoint(image, model_index) # params are not used in v2
        return predictions

    def get_pred_types(self, predictions, incl_user_boxes=False):
        """ Returns the types of predictions returned by the Detect method
        
        Params:
            predictions: dict, as described in DetectionModel.detect
        
        Returns:
            result: list, that contains the type of predictions ("box" for bouding boxes, 
                    "mask" for segmentation result)
        """
        result = list()
        if incl_user_boxes and "boxes_adj" in predictions:
            if predictions["boxes_adj"] != []: result.append("box")
        else:
            if predictions["boxes"] != []: result.append("box")
        if predictions["masks"] != []: result.append("mask")
        return result

    def get_pred_classes(self, predictions, incl_user_boxes=False):
        """ Returns the class names returnd by the Detect method

        Params:
            predictions: dict, as described in DetectionModel.detect
            incl_user_boxes, bool (default False)
        
        Returns:
            result: list, that contains the class names (without duplicates)
        """
        if incl_user_boxes and "boxes_adj" in predictions:
            return predictions["pred_labels_adj"]
        else:
            return predictions["pred_labels"]

    def get_instance_ids(self, class_name, predictions, incl_user_boxes=False):
        """ Returns the instances ids of for a given class

        Params:
            class_name: str, name of the class
            predictions: dict, as described in DetectionModel.detect
            incl_user_boxes, bool (default False)
        
        Returns:
            result: list, that contains "all" (to represent all ids) and the instance ids
        """
        result = ["all"]
        class_id = predictions["name2int"][class_name]
        if incl_user_boxes and "boxes_adj" in predictions:
            pred_classes = predictions["pred_classes_adj"]
            i_ids = predictions["instance_ids_adj"]
        else:
            pred_classes = predictions["pred_classes"]
            i_ids = predictions["instance_ids"]
        mask = [True if i == class_id else False for i in pred_classes]
        instance_ids = list(compress(i_ids,mask))
        if len(instance_ids) > 1: result.extend(instance_ids)
        return result

    def get_target_regions(self, class_name, instance_id, target_type, predictions, incl_user_boxes=False):
        """ Returns the target regions in the image 
        
        Params:
            class_name: str, the name of the classe that the user wants to anonymise
            instance_id: str, "all" or the id of the instance within the class
            target_type: str, "box" or "mask"
            predictions: dict, as described in DetectionModel.detect
            incl_user_boxes, bool (default False)

        Returns:
            result: numpy array, indices of the target pixels in the image
        """
        class_id = predictions["name2int"][class_name]
        if target_type == "box":
            if incl_user_boxes and "boxes_adj" in predictions:
                boxes = predictions["boxes_adj"]
                pred_classes = predictions["pred_classes_adj"]
            else:
                boxes = predictions["boxes"]
                pred_classes = predictions["pred_classes"] 
            mask = np.array([True if i == class_id else False for i in pred_classes])
            boxes = np.array(boxes)[mask]
            if instance_id != "all":
                boxes = np.array([boxes[int(instance_id)]])
            result = self._get_indices_from_boxes(boxes)
        elif target_type == "mask":
            mask = np.array([True if i == class_id else False for i in predictions["pred_classes"]])
            seg_masks = np.array(predictions["masks"])[mask]
            if instance_id != "all":
                seg_masks = np.array([seg_masks[int(instance_id)]])
            seg_mask = np.sum(seg_masks, axis=0)
            result = np.where(seg_mask != 0)

        return result

    def _get_indices_from_boxes(self, boxes):
        """ Helper function to convert box coordinates into pixels
        """
        tmp = list()
        box_indices_y = list()
        box_indices_x = list()
        for box in boxes:
            x1, y1, x2, y2 = box
            tmp.extend((y,x) for y in range(y1,y2) for x in range(x1,x2))
        box_indices_y = [t[0] for t in tmp]
        box_indices_x = [t[1] for t in tmp]
        return (np.array(box_indices_y), np.array(box_indices_x))

    def visualise_boxes(self, image, predictions, incl_user_boxes = False):
        """ Function used to visualise boxes detected   
        """
        output = np.copy(image)
        if incl_user_boxes and "boxes_adj" in predictions:
            boxes = predictions["boxes_adj"]
            pred_classes = predictions["pred_classes_adj"]
            instance_ids = predictions["instance_ids_adj"]
            pred_labels = predictions["pred_labels_adj"]
        else:
            boxes = predictions["boxes"]
            pred_classes = predictions["pred_classes"]
            instance_ids = predictions["instance_ids"]
            pred_labels = predictions["pred_labels"]
        color_map = {predictions["name2int"][name]:(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)) 
                            for name in pred_labels} 
        colors = [color_map[id] for id in pred_classes]
        labels = list()
        multi_class = len(pred_labels) > 1
        for c_id,i_id in zip(pred_classes, instance_ids):
            if multi_class:
                labels.append(f'{predictions["class_names"][c_id]}_{i_id}')
            else:
                labels.append(f'{i_id}')
        for box,color,label in zip(boxes,colors,labels):
            x1, y1, x2, y2 = box
            cv2.rectangle(output, (x1, y1), (x2, y2), color, 2)
            cv2.putText(output, label, (x1,y1), cv2.FONT_HERSHEY_SIMPLEX, 1, color, 2, 1)
        return output

    def add_labeled_box(self, box, label, predictions):
        """ Adds a user defined box to the predictions dictionary

        Params:
            box: list, box coordinates [x1, y1, x2, y2]
            label: str, class name that the box belongs to (should already be in predictions["class_names"])
            predictions: dict, as described in DetectionModel.detect

        Returns:
            predictions: dict, this is the input dictionary with additional key/value items:
                "user_boxes": The labels provided by the user
                "pred_classes_Adj": Ids of the classes detected including the ones added by the user
                "scores_adj: The original scores including 1 (100% certainty) for each user label
                "boxes_adj": The original boxes including the ones added by the user
                "instance_ids_adj": The original instance ids including the ones of the new user objects  
        """
        if label in predictions["class_names"]:
            label_id = predictions["name2int"][label]
            if "user_boxes" in predictions:
                predictions["user_boxes"].append(box)
            else:
                predictions["user_boxes"] = [box]
                predictions["pred_classes_adj"] = predictions["pred_classes"].copy()
                predictions["scores_adj"] = predictions["scores"].copy() 
                predictions["boxes_adj"] = predictions["boxes"].copy()
                predictions["instance_ids_adj"] = predictions["instance_ids"].copy()
                predictions["pred_labels_adj"] = predictions["pred_labels"].copy()
            predictions["scores_adj"].append(1)
            predictions["boxes_adj"].append(box)
            if label not in predictions["pred_labels_adj"]:
                predictions["pred_labels_adj"].append(label)
            if len(predictions["class_names"]) > 1:
                new_id = 0
                if new_id in predictions["pred_classes_adj"]:
                    new_id = sum(1 if label_id == c else 0 for c in predictions["pred_classes_adj"])
            else:
                new_id = len(predictions["instance_ids_adj"])
            predictions["pred_classes_adj"].append(label_id)
            predictions["instance_ids_adj"].append(new_id)
        return predictions

    def _predict_from_endpoint(self, image, model_index):
        pil_img = PIL.Image.fromarray(image)
        buff = BytesIO()
        pil_img.save(buff, format="JPEG")
        byte_string = base64.b64encode(buff.getvalue()).decode("utf-8")
        response = requests.post(f"{self.backend_url}/detect", json={"image_str": byte_string, 
                                                                "model_index": model_index})
        predictions = response.json()["predictions"]
        return predictions

    def get_endpoint_info(self):
        response = requests.get(f"{self.backend_url}/")
        info = response.json()
        self.choices = info["choices"]
        self.descriptions = info["descriptions"]
        self.classes = info["classes"]


