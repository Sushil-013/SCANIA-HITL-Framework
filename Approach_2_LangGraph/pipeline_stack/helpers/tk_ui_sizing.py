STANDARD_WINDOW_WIDTH = 1360
STANDARD_WINDOW_HEIGHT = 820
STANDARD_WINDOW_MIN_WIDTH = 1180
STANDARD_WINDOW_MIN_HEIGHT = 720

STANDARD_REVIEW_PREVIEW_WIDTH = 860
STANDARD_REVIEW_PREVIEW_HEIGHT = 700


def _safe_screen_size(root):
    try:
        return int(root.winfo_screenwidth()), int(root.winfo_screenheight())
    except Exception:
        return 1920, 1080


def apply_standard_window(
    root,
    width=STANDARD_WINDOW_WIDTH,
    height=STANDARD_WINDOW_HEIGHT,
    min_width=STANDARD_WINDOW_MIN_WIDTH,
    min_height=STANDARD_WINDOW_MIN_HEIGHT,
):
    screen_width, screen_height = _safe_screen_size(root)
    resolved_width = max(min_width, min(int(width), max(min_width, screen_width - 80)))
    resolved_height = max(min_height, min(int(height), max(min_height, screen_height - 120)))

    try:
        root.minsize(int(min_width), int(min_height))
    except Exception:
        pass

    x = max(20, int((screen_width - resolved_width) / 2))
    y = max(20, int((screen_height - resolved_height) / 2))
    root.geometry("{}x{}+{}+{}".format(resolved_width, resolved_height, x, y))
    return resolved_width, resolved_height
