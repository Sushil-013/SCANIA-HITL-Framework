import argparse
import base64
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from collections import OrderedDict

import cv2
import numpy as np
import torch
import torch.backends.cudnn as cudnn
from PIL import Image, ImageDraw
from torch.autograd import Variable
from transformers import AutoModelForImageTextToText, AutoProcessor

from . import craft_utils, file_utils, imgproc
from .craft import CRAFT


def copy_state_dict(state_dict):
    if list(state_dict.keys())[0].startswith("module"):
        start_idx = 1
    else:
        start_idx = 0

    new_state_dict = OrderedDict()
    for key, value in state_dict.items():
        name = ".".join(key.split(".")[start_idx:])
        new_state_dict[name] = value
    return new_state_dict


def str2bool(value):
    return value.lower() in ("yes", "y", "true", "t", "1")


def parse_args():
    parser = argparse.ArgumentParser(description="CRAFT + GLM-OCR pipeline")
    parser.add_argument("--image", required=True, type=str, help="path to a single input image")
    parser.add_argument("--document_id", default=None, type=str, help="stable document id for structured outputs")
    parser.add_argument("--trained_model", default="weights/craft_mlt_25k.pth", type=str, help="CRAFT weights")
    parser.add_argument("--text_threshold", default=0.7, type=float, help="text confidence threshold")
    parser.add_argument("--low_text", default=0.4, type=float, help="text low-bound score")
    parser.add_argument("--link_threshold", default=0.4, type=float, help="link confidence threshold")
    parser.add_argument("--cuda", default=True, type=str2bool, help="use CUDA for CRAFT inference")
    parser.add_argument("--canvas_size", default=1280, type=int, help="image size for CRAFT inference")
    parser.add_argument("--mag_ratio", default=1.5, type=float, help="image magnification ratio")
    parser.add_argument("--poly", default=False, action="store_true", help="use polygon crops when available")
    parser.add_argument("--show_time", default=False, action="store_true", help="show CRAFT timing")
    parser.add_argument("--refine", default=False, action="store_true", help="enable link refiner")
    parser.add_argument("--refiner_model", default="weights/craft_refiner_CTW1500.pth", type=str, help="refiner weights")
    parser.add_argument("--glm_model", default="zai-org/GLM-OCR", type=str, help="GLM-OCR model id")
    parser.add_argument("--max_new_tokens", default=2048, type=int, help="maximum GLM-OCR output tokens")
    parser.add_argument("--result_folder", default="./result/", type=str, help="base directory for CRAFT and OCR outputs")
    parser.add_argument("--schema_version", default="1.0", type=str, help="schema version for structured outputs")
    parser.add_argument("--enable_view_assignment", default=False, action="store_true", help="use OpenAI vision to group dimensions by view")
    parser.add_argument("--openai_model", default="gpt-4.1", type=str, help="OpenAI vision model for view assignment")
    parser.add_argument("--openai_api_key", default=None, type=str, help="OpenAI API key; defaults to OPENAI_API_KEY env var")
    return parser.parse_args()


def load_craft_model(args):
    use_cuda = bool(args.cuda and torch.cuda.is_available())
    if args.cuda and not use_cuda:
        print("CUDA was requested for CRAFT but is not available. Falling back to CPU.")

    net = CRAFT()
    print("Loading CRAFT weights from checkpoint ({})".format(args.trained_model))
    if use_cuda:
        net.load_state_dict(copy_state_dict(torch.load(args.trained_model)))
        net = net.cuda()
        net = torch.nn.DataParallel(net)
        cudnn.benchmark = False
    else:
        net.load_state_dict(copy_state_dict(torch.load(args.trained_model, map_location="cpu")))
    net.eval()

    refine_net = None
    if args.refine:
        from .refinenet import RefineNet

        refine_net = RefineNet()
        args.poly = True
        print("Loading refiner weights from checkpoint ({})".format(args.refiner_model))
        if use_cuda:
            refine_net.load_state_dict(copy_state_dict(torch.load(args.refiner_model)))
            refine_net = refine_net.cuda()
            refine_net = torch.nn.DataParallel(refine_net)
        else:
            refine_net.load_state_dict(copy_state_dict(torch.load(args.refiner_model, map_location="cpu")))
        refine_net.eval()

    return net, refine_net, use_cuda


def load_glmocr_model(model_name):
    processor = AutoProcessor.from_pretrained(model_name)
    model = AutoModelForImageTextToText.from_pretrained(
        model_name,
        torch_dtype="auto",
        device_map="auto",
    )
    return processor, model


def detect_text_with_craft(image, net, args, refine_net=None, use_cuda=False):
    start_time = cv2.getTickCount()

    img_resized, target_ratio, size_heatmap = imgproc.resize_aspect_ratio(
        image,
        args.canvas_size,
        interpolation=cv2.INTER_LINEAR,
        mag_ratio=args.mag_ratio,
    )
    ratio_h = ratio_w = 1 / target_ratio

    x = imgproc.normalizeMeanVariance(img_resized)
    x = torch.from_numpy(x).permute(2, 0, 1)
    x = Variable(x.unsqueeze(0))
    if use_cuda:
        x = x.cuda()

    with torch.no_grad():
        y, feature = net(x)

    score_text = y[0, :, :, 0].cpu().data.numpy()
    score_link = y[0, :, :, 1].cpu().data.numpy()

    if refine_net is not None:
        with torch.no_grad():
            y_refiner = refine_net(y, feature)
        score_link = y_refiner[0, :, :, 0].cpu().data.numpy()

    boxes, polys = craft_utils.getDetBoxes(
        score_text,
        score_link,
        args.text_threshold,
        args.link_threshold,
        args.low_text,
        args.poly,
    )

    boxes = craft_utils.adjustResultCoordinates(boxes, ratio_w, ratio_h)
    polys = craft_utils.adjustResultCoordinates(polys, ratio_w, ratio_h)

    crop_shapes = []
    for idx, box in enumerate(boxes):
        polygon = polys[idx] if idx < len(polys) and polys[idx] is not None else box
        crop_shapes.append(np.asarray(polygon, dtype=np.float32))
        boxes[idx] = np.asarray(box, dtype=np.float32)

    render_img = np.hstack((score_text.copy(), score_link))
    score_heatmap = imgproc.cvt2HeatmapImg(render_img)

    if args.show_time:
        elapsed = (cv2.getTickCount() - start_time) / cv2.getTickFrequency()
        print("CRAFT infer/postproc time: {:.3f}s".format(elapsed))

    return boxes, crop_shapes, score_heatmap


def crop_regions(image, boxes):
    crops = []
    width, height = image.size

    for box in boxes:
        points = np.asarray(box, dtype=np.float32).reshape(-1, 2)
        polygon = [(float(x), float(y)) for x, y in points]

        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).polygon(polygon, fill=255)

        white_background = Image.new("RGB", image.size, (255, 255, 255))
        masked_image = Image.composite(image, white_background, mask)

        min_x = max(0, int(np.floor(points[:, 0].min())))
        min_y = max(0, int(np.floor(points[:, 1].min())))
        max_x = min(width, int(np.ceil(points[:, 0].max())))
        max_y = min(height, int(np.ceil(points[:, 1].max())))

        if max_x <= min_x:
            max_x = min(width, min_x + 1)
        if max_y <= min_y:
            max_y = min(height, min_y + 1)

        crops.append(masked_image.crop((min_x, min_y, max_x, max_y)))

    return crops


def recognize_with_glmocr(crop, processor, model, max_new_tokens=2048):
    fd, temp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)

    try:
        crop.save(temp_path)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "url": temp_path},
                    {"type": "text", "text": "Text Recognition:"},
                ],
            }
        ]

        inputs = processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        ).to(model.device)
        inputs.pop("token_type_ids", None)

        with torch.no_grad():
            generated_ids = model.generate(**inputs, max_new_tokens=max_new_tokens)

        text = processor.decode(
            generated_ids[0][inputs["input_ids"].shape[1]:],
            skip_special_tokens=True,
        )
        return text.strip()
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def assemble_json(boxes, texts):
    output = []
    for box, text in zip(boxes, texts):
        quad = np.asarray(box, dtype=np.float32).reshape(-1, 2)
        if quad.shape[0] != 4:
            rect = cv2.minAreaRect(quad.astype(np.float32))
            quad = cv2.boxPoints(rect)

        bbox = [int(round(coord)) for point in quad[:4] for coord in point.tolist()]
        output.append({"bbox": bbox, "text": text})
    return output


def bbox_to_rect(bbox):
    quad = np.asarray(bbox, dtype=np.float32).reshape(-1, 2)
    min_x = int(round(np.min(quad[:, 0])))
    min_y = int(round(np.min(quad[:, 1])))
    max_x = int(round(np.max(quad[:, 0])))
    max_y = int(round(np.max(quad[:, 1])))
    return [min_x, min_y, max_x, max_y]


def rect_to_bbox(rect):
    min_x, min_y, max_x, max_y = rect
    return [min_x, min_y, max_x, min_y, max_x, max_y, min_x, max_y]


def union_rect(rects):
    min_x = min(rect[0] for rect in rects)
    min_y = min(rect[1] for rect in rects)
    max_x = max(rect[2] for rect in rects)
    max_y = max(rect[3] for rect in rects)
    return [min_x, min_y, max_x, max_y]


def normalize_ocr_text(text):
    normalized = text or ""
    replacements = {
        "\\Phi": "\u03A6",
        "\\phi": "\u03A6",
        "\\pm": "\u00B1",
        "$": "",
        "{": "",
        "}": "",
    }
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)

    normalized = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", normalized)
    normalized = re.sub(r"(?<=\d)\s+(?=\d)", "", normalized)
    normalized = re.sub(r"\s*\u00B1\s*", " \u00B1 ", normalized)
    normalized = re.sub(r"\s*\+\s*", " +", normalized)
    normalized = re.sub(r"\s*-\s*", " -", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def make_raw_items(ocr_results):
    raw_items = []
    for idx, item in enumerate(ocr_results, start=1):
        bbox = item["bbox"]
        rect = bbox_to_rect(bbox)
        raw_items.append(
            {
                "ocr_id": "OCR_{:03d}".format(idx),
                "bbox": bbox,
                "rect": rect,
                "text": item["text"],
                "normalized_text": normalize_ocr_text(item["text"]),
            }
        )
    return raw_items


def should_merge_items(current_group, next_item):
    current_rect = current_group["rect"]
    next_rect = next_item["rect"]

    current_height = max(1, current_rect[3] - current_rect[1])
    next_height = max(1, next_rect[3] - next_rect[1])
    current_center_y = (current_rect[1] + current_rect[3]) / 2.0
    next_center_y = (next_rect[1] + next_rect[3]) / 2.0
    vertical_diff = abs(current_center_y - next_center_y)
    horizontal_gap = next_rect[0] - current_rect[2]

    same_line = vertical_diff <= max(current_height, next_height) * 0.75
    close_horizontally = horizontal_gap <= max(24, max(current_height, next_height) * 1.5)
    overlapping = next_rect[0] <= current_rect[2]

    return same_line and (close_horizontally or overlapping)


def merge_raw_items(raw_items):
    if not raw_items:
        return []

    sorted_items = sorted(raw_items, key=lambda item: (item["rect"][1], item["rect"][0]))
    groups = []

    for item in sorted_items:
        if not groups:
            groups.append(
                {
                    "source_ocr_ids": [item["ocr_id"]],
                    "texts": [item["normalized_text"]],
                    "rect": item["rect"],
                }
            )
            continue

        last_group = groups[-1]
        if should_merge_items(last_group, item):
            last_group["source_ocr_ids"].append(item["ocr_id"])
            last_group["texts"].append(item["normalized_text"])
            last_group["rect"] = union_rect([last_group["rect"], item["rect"]])
        else:
            groups.append(
                {
                    "source_ocr_ids": [item["ocr_id"]],
                    "texts": [item["normalized_text"]],
                    "rect": item["rect"],
                }
            )

    merged = []
    for idx, group in enumerate(groups, start=1):
        raw_text = " ".join(text for text in group["texts"] if text).strip()
        normalized_text = normalize_ocr_text(raw_text)
        type_hint = "diameter" if any(symbol in normalized_text for symbol in ("\u03A6", "\u2300", "\u00D8")) else "linear"
        merged.append(
            {
                "candidate_id": "CAND_{:03d}".format(idx),
                "bbox": rect_to_bbox(group["rect"]),
                "raw_text": raw_text,
                "normalized_text": normalized_text,
                "dimension_type_hint": type_hint,
                "source_ocr_ids": group["source_ocr_ids"],
            }
        )
    return merged


def extract_first_number(text):
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0))


def parse_dimension_candidate(candidate):
    text = candidate["normalized_text"]
    compact_text = text.replace(" ", "")
    dimension_type = "linear"
    if any(symbol in compact_text for symbol in ("\u03A6", "\u2300", "\u00D8")):
        dimension_type = "diameter"
    elif compact_text.startswith("R"):
        dimension_type = "radius"

    nominal_value = None
    tolerance_plus = None
    tolerance_minus = None
    status = "parsed"

    plus_minus_match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*\u00B1\s*(\d+(?:\.\d+)?)", text)
    stacked_tolerance_match = re.search(
        r"([-+]?\d+(?:\.\d+)?)\s*\+\s*(\d+(?:\.\d+)?)\s*/\s*-\s*(\d+(?:\.\d+)?)",
        compact_text,
    )

    value_text = re.sub(r"[\u03A6\u2300\u00D8R]", "", text).strip()
    if plus_minus_match:
        nominal_value = float(plus_minus_match.group(1))
        tolerance_plus = float(plus_minus_match.group(2))
        tolerance_minus = float(plus_minus_match.group(2))
    elif stacked_tolerance_match:
        nominal_value = float(stacked_tolerance_match.group(1))
        tolerance_plus = float(stacked_tolerance_match.group(2))
        tolerance_minus = float(stacked_tolerance_match.group(3))
    else:
        nominal_value = extract_first_number(value_text)

    if nominal_value is None:
        status = "uncertain"
        dimension_type = "unknown"

    return {
        "candidate_id": candidate["candidate_id"],
        "dimension_type": dimension_type,
        "nominal_value": nominal_value,
        "unit": "mm",
        "tolerance_plus": tolerance_plus,
        "tolerance_minus": tolerance_minus,
        "raw_text": candidate["raw_text"],
        "normalized_text": text,
        "bbox": candidate["bbox"],
        "source_ocr_ids": candidate["source_ocr_ids"],
        "status": status,
    }


def build_structured_document(image_path, args, raw_items, merged_candidates):
    document_id = args.document_id or os.path.splitext(os.path.basename(image_path))[0]
    parsed_dimensions = []
    unclassified = []

    for idx, candidate in enumerate(merged_candidates, start=1):
        parsed = parse_dimension_candidate(candidate)
        parsed["item_id"] = "DIM_{:03d}".format(idx)
        if parsed["status"] == "parsed":
            parsed_dimensions.append(parsed)
        else:
            unclassified.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "raw_text": candidate["raw_text"],
                    "normalized_text": candidate["normalized_text"],
                    "bbox": candidate["bbox"],
                    "source_ocr_ids": candidate["source_ocr_ids"],
                    "reason": "Could not parse dimension value from OCR text.",
                }
            )

    document = {
        "document_id": document_id,
        "source_image": image_path,
        "schema_version": args.schema_version,
        "raw_items": [
            {
                "ocr_id": item["ocr_id"],
                "bbox": item["bbox"],
                "text": item["text"],
                "normalized_text": item["normalized_text"],
            }
            for item in raw_items
        ],
        "merged_candidates": merged_candidates,
        "sections": [
            {
                "section_id": "SEC_001",
                "section_type": "dimensions",
                "items": parsed_dimensions,
            }
        ],
        "unclassified_candidates": unclassified,
    }
    return document


def encode_image_as_data_url(image_path):
    ext = os.path.splitext(image_path)[1].lower()
    mime_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/png")

    with open(image_path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("utf-8")
    return "data:{};base64,{}".format(mime_type, encoded)


def get_view_assignment_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["document_id", "views", "unassigned_candidates"],
        "properties": {
            "document_id": {"type": "string"},
            "views": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["view_id", "view_type", "view_bbox", "assigned_dimensions"],
                    "properties": {
                        "view_id": {"type": "string"},
                        "view_type": {
                            "type": "string",
                            "enum": ["front", "top", "side", "section", "detail", "unknown"],
                        },
                        "view_bbox": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "assigned_dimensions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "dimension_id",
                                    "candidate_id",
                                    "raw_text",
                                    "normalized_text",
                                    "dimension_type",
                                    "nominal_value",
                                    "tolerance_plus",
                                    "tolerance_minus",
                                    "status",
                                ],
                                "properties": {
                                    "dimension_id": {"type": "string"},
                                    "candidate_id": {"type": "string"},
                                    "raw_text": {"type": "string"},
                                    "normalized_text": {"type": "string"},
                                    "dimension_type": {
                                        "type": "string",
                                        "enum": ["linear", "diameter", "radius", "unknown"],
                                    },
                                    "nominal_value": {"type": ["number", "null"]},
                                    "tolerance_plus": {"type": ["number", "null"]},
                                    "tolerance_minus": {"type": ["number", "null"]},
                                    "status": {"type": "string", "enum": ["parsed", "uncertain"]},
                                },
                            },
                        },
                    },
                },
            },
            "unassigned_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["candidate_id", "reason"],
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    }


def extract_text_from_openai_response(response_json):
    if isinstance(response_json.get("output_text"), str) and response_json["output_text"].strip():
        return response_json["output_text"]

    texts = []
    for output in response_json.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "refusal":
                raise RuntimeError(content.get("refusal", "OpenAI refused the request."))
            if content.get("type") in ("output_text", "text"):
                text_value = content.get("text")
                if isinstance(text_value, str):
                    texts.append(text_value)

    if texts:
        return "".join(texts)
    raise RuntimeError("OpenAI response did not include text output.")


def assign_views_with_openai(image_path, structured_document, args):
    api_key = args.openai_api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OpenAI API key not found. Set OPENAI_API_KEY or pass --openai_api_key.")

    candidate_payload = [
        {
            "candidate_id": candidate["candidate_id"],
            "bbox": candidate["bbox"],
            "raw_text": candidate["raw_text"],
            "normalized_text": candidate["normalized_text"],
            "dimension_type_hint": candidate["dimension_type_hint"],
            "source_ocr_ids": candidate["source_ocr_ids"],
        }
        for candidate in structured_document["merged_candidates"]
    ]

    instructions = (
        "You are an engineering drawing layout parser. "
        "Identify the major drawing views and assign each dimension candidate to the correct view. "
        "Use the image for layout understanding and the candidate JSON for dimension text. "
        "Do not invent dimensions. If uncertain, put items in unassigned_candidates."
    )
    user_text = (
        "Return strict JSON only. "
        "Document ID: {document_id}\n"
        "Assign the candidates to drawing views labeled as front, top, side, section, detail, or unknown.\n"
        "Merged dimension candidates:\n{candidates}"
    ).format(
        document_id=structured_document["document_id"],
        candidates=json.dumps(candidate_payload, ensure_ascii=False, indent=2),
    )

    payload = {
        "model": args.openai_model,
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_text},
                    {
                        "type": "input_image",
                        "image_url": encode_image_as_data_url(image_path),
                        "detail": "high",
                    },
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "drawing_view_assignment",
                "strict": True,
                "schema": get_view_assignment_schema(),
            }
        },
    }

    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer {}".format(api_key),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("OpenAI request failed: {}".format(error_body))

    return json.loads(extract_text_from_openai_response(response_json))


def prepare_result_folders(result_folder):
    base_folder = result_folder.rstrip("/\\")
    craft_folder = os.path.join(base_folder, "craft")
    ocr_folder = os.path.join(base_folder, "ocr")
    openai_folder = os.path.join(base_folder, "openai")
    os.makedirs(craft_folder, exist_ok=True)
    os.makedirs(ocr_folder, exist_ok=True)
    os.makedirs(openai_folder, exist_ok=True)
    return craft_folder, ocr_folder, openai_folder


def save_craft_outputs(image_path, original_image, boxes_for_draw, score_heatmap, craft_folder):
    filename = os.path.splitext(os.path.basename(image_path))[0]
    mask_path = os.path.join(craft_folder, "res_{}_mask.jpg".format(filename))

    cv2.imwrite(mask_path, score_heatmap)
    file_utils.saveResult(image_path, original_image[:, :, ::-1], boxes_for_draw, dirname=craft_folder.rstrip("/\\") + os.sep)


def save_ocr_outputs(image_path, crops, results, ocr_folder):
    filename = os.path.splitext(os.path.basename(image_path))[0]
    image_ocr_folder = os.path.join(ocr_folder, filename)
    os.makedirs(image_ocr_folder, exist_ok=True)

    for idx, crop in enumerate(crops):
        crop_path = os.path.join(image_ocr_folder, "crop_{:03d}.png".format(idx))
        crop.save(crop_path)

    json_path = os.path.join(image_ocr_folder, "{}_glmocr.json".format(filename))
    with open(json_path, "w", encoding="utf-8") as json_file:
        json.dump(results, json_file, ensure_ascii=False, indent=2)

    return json_path


def save_structured_outputs(image_path, raw_items, merged_candidates, structured_document, ocr_folder, openai_folder, view_assignment=None):
    filename = os.path.splitext(os.path.basename(image_path))[0]
    image_ocr_folder = os.path.join(ocr_folder, filename)
    os.makedirs(image_ocr_folder, exist_ok=True)

    output_paths = {
        "raw_ocr": os.path.join(image_ocr_folder, "{}_raw_ocr.json".format(filename)),
        "merged_candidates": os.path.join(image_ocr_folder, "{}_merged_candidates.json".format(filename)),
        "structured_dimensions": os.path.join(image_ocr_folder, "{}_structured_dimensions.json".format(filename)),
    }

    raw_output = [
        {
            "ocr_id": item["ocr_id"],
            "bbox": item["bbox"],
            "text": item["text"],
            "normalized_text": item["normalized_text"],
        }
        for item in raw_items
    ]

    with open(output_paths["raw_ocr"], "w", encoding="utf-8") as raw_file:
        json.dump(raw_output, raw_file, ensure_ascii=False, indent=2)

    with open(output_paths["merged_candidates"], "w", encoding="utf-8") as candidate_file:
        json.dump(merged_candidates, candidate_file, ensure_ascii=False, indent=2)

    with open(output_paths["structured_dimensions"], "w", encoding="utf-8") as structured_file:
        json.dump(structured_document, structured_file, ensure_ascii=False, indent=2)

    if view_assignment is not None:
        image_openai_folder = os.path.join(openai_folder, filename)
        os.makedirs(image_openai_folder, exist_ok=True)
        view_path = os.path.join(image_openai_folder, "{}_view_assignment.json".format(filename))
        with open(view_path, "w", encoding="utf-8") as view_file:
            json.dump(view_assignment, view_file, ensure_ascii=False, indent=2)
        output_paths["view_assignment"] = view_path

    return output_paths


def main():
    args = parse_args()
    image_path = os.path.abspath(args.image)
    pil_image = Image.open(image_path).convert("RGB")
    np_image = np.array(pil_image)

    craft_net, refine_net, use_cuda = load_craft_model(args)
    processor, glm_model = load_glmocr_model(args.glm_model)

    boxes, crop_shapes, score_heatmap = detect_text_with_craft(
        np_image,
        craft_net,
        args,
        refine_net=refine_net,
        use_cuda=use_cuda,
    )

    crops = crop_regions(pil_image, crop_shapes)
    texts = [recognize_with_glmocr(crop, processor, glm_model, args.max_new_tokens) for crop in crops]
    results = assemble_json(boxes, texts)
    raw_items = make_raw_items(results)
    merged_candidates = merge_raw_items(raw_items)
    structured_document = build_structured_document(image_path, args, raw_items, merged_candidates)

    craft_folder, ocr_folder, openai_folder = prepare_result_folders(args.result_folder)
    save_craft_outputs(image_path, np_image, crop_shapes, score_heatmap, craft_folder)
    save_ocr_outputs(image_path, crops, results, ocr_folder)
    view_assignment = None
    if args.enable_view_assignment:
        view_assignment = assign_views_with_openai(image_path, structured_document, args)
    save_structured_outputs(
        image_path,
        raw_items,
        merged_candidates,
        structured_document,
        ocr_folder,
        openai_folder,
        view_assignment=view_assignment,
    )

    final_output = {
        "ocr_results": results,
        "structured_document": structured_document,
    }
    if view_assignment is not None:
        final_output["view_assignment"] = view_assignment

    print(json.dumps(final_output, ensure_ascii=False, indent=2))
    return final_output


if __name__ == "__main__":
    main()
