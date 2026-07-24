#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dドライブなどのディスク使用量をツリーマップで可視化するツール。

- Python 3.7 / 標準ライブラリのみ
- Windows の長いパス (MAX_PATH 超え) に対応
- フォルダをクリックすると下位階層にズーム
- マウス移動でパス・容量をステータス欄に表示

使い方 (Windows):
    python disk_treemap.py --path D:\

使い方 (Linux / macOS):
    python disk_treemap.py --path /home
"""

import argparse
import colorsys
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont
from typing import List, Optional, Tuple

import zlib


class FolderNode:
    __slots__ = ('path', 'name', 'size', 'children')

    def __init__(self, path: str, name: str) -> None:
        self.path = path
        self.name = name
        self.size = 0
        self.children: List['FolderNode'] = []


def normalize_path(path: str) -> str:
    """Windows で MAX_PATH を超えるパスに対応するための \\?\ プレフィックスを付ける。"""
    p = os.path.abspath(path)
    if sys.platform != 'win32':
        return p
    if p.startswith('\\\\?\\') or p.startswith('\\\\?\\UNC\\') or p.startswith('\\\\.\\'):
        return p
    return '\\\\?\\' + p


def display_path(path: str) -> str:
    """\\?\ プレフィックスを取り除いて表示しやすくする。"""
    if path.startswith('\\\\?\\UNC\\'):
        return '\\\\' + path[8:]
    if path.startswith('\\\\?\\'):
        return path[4:]
    return path


def base_name(path: str) -> str:
    p = path.rstrip(os.sep)
    name = os.path.basename(p)
    return name if name else display_path(path)


def human_readable(size: int) -> str:
    """バイト数を人間が読みやすい単位に変換する。"""
    units = ('B', 'KB', 'MB', 'GB', 'TB', 'PB')
    value = float(size)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            if unit == 'B':
                return f'{int(value)} B'
            return f'{value:.2f} {unit}'
        value /= 1024.0
    return f'{size} B'


def scan_directory(root_path: str, status_callback) -> FolderNode:
    """
    指定したパス以下のフォルダ容量を再帰的に集計する。
    アクセス権がないフォルダはスキップし、進捗を status_callback に通知する。
    """
    root_path = normalize_path(root_path)
    if not os.path.isdir(root_path):
        raise NotADirectoryError(root_path)

    root = FolderNode(root_path, base_name(root_path))
    order: List[FolderNode] = []
    stack: List[FolderNode] = [root]

    while stack:
        node = stack.pop()
        order.append(node)
        try:
            with os.scandir(node.path) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            try:
                                node.size += entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                pass
                        elif entry.is_dir(follow_symlinks=False):
                            child = FolderNode(entry.path, entry.name)
                            node.children.append(child)
                            stack.append(child)
                    except OSError:
                        continue
        except OSError as e:
            if status_callback:
                status_callback(f'アクセス不可: {display_path(node.path)} ({e})')

    # 下から上へ子フォルダの容量を加算し、描画用に子を大きい順に並べる
    for node in reversed(order):
        for child in node.children:
            node.size += child.size
        node.children.sort(key=lambda n: n.size, reverse=True)

    return root


# ---- ツリーマップの Squarified レイアウト ----


def _worst(row: List[float], w: float, h: float) -> float:
    """現在の行に次の要素を加えると縦横比がどう悪化するかを評価する。"""
    if not row:
        return float('inf')
    s = sum(row)
    if s == 0 or row[0] == 0:
        return float('inf')
    if w >= h:
        return max(h * h * row[-1] / (s * s), s * s / (w * w * row[0]))
    return max(w * w * row[-1] / (s * s), s * s / (h * h * row[0]))


def _layout_row(row: List[float], x: float, y: float, w: float, h: float
                ) -> Tuple[List[Tuple[float, float, float, float]], float, float, float, float]:
    """1行分の矩形を確定し、残りの領域を返す。"""
    s = sum(row)
    rects: List[Tuple[float, float, float, float]] = []
    if w >= h:
        row_h = s / w if w > 0 else 0
        cx = x
        for v in row:
            rw = v / row_h if row_h > 0 else 0
            rects.append((cx, y, rw, row_h))
            cx += rw
        return rects, x, y + row_h, w, h - row_h
    else:
        row_w = s / h if h > 0 else 0
        cy = y
        for v in row:
            rh = v / row_w if row_w > 0 else 0
            rects.append((x, cy, row_w, rh))
            cy += rh
        return rects, x + row_w, y, w - row_w, h


def squarify(values: List[float], x: float, y: float, w: float, h: float
             ) -> List[Tuple[float, float, float, float]]:
    """
    与えられた値を面積に比例する長方形に分割し、(x, y, w, h) のリストを返す。
    Bruls et al. の Squarified Treemap アルゴリズムを実装。
    """
    if not values or w <= 0 or h <= 0:
        return []

    total = sum(values)
    if total == 0:
        # 全て 0 の場合は均等に配置
        values = [1.0] * len(values)
        total = float(len(values))

    area = w * h
    norm = [v / total * area for v in values]

    rects: List[Tuple[float, float, float, float]] = []
    row: List[float] = []
    i = 0
    n = len(norm)

    while i < n or row:
        if i >= n:
            row_rects, x, y, w, h = _layout_row(row, x, y, w, h)
            rects.extend(row_rects)
            break

        v = norm[i]
        if row and _worst(row + [v], w, h) >= _worst(row, w, h):
            row_rects, x, y, w, h = _layout_row(row, x, y, w, h)
            rects.extend(row_rects)
            row = []
        else:
            row.append(v)
            i += 1

    return rects


# ---- GUI ----


class TreemapApp:
    PAD = 4
    MIN_RECT_SIZE = 4

    def __init__(self, root: tk.Tk, target_path: str) -> None:
        self.root = root
        self.target_path = target_path
        self.root_node: Optional[FolderNode] = None
        self.current_node: Optional[FolderNode] = None
        self.history: List[FolderNode] = []
        self.rectangles: List[Tuple[float, float, float, float, FolderNode]] = []
        self.font = tkfont.Font(family='Helvetica', size=9)
        self.small_font = tkfont.Font(family='Helvetica', size=8)
        self._resize_job: Optional[int] = None

        self.root.title('Disk Treemap')
        self.root.geometry('1024x768')
        self.root.minsize(400, 300)

        # ヘッダー
        self.header = tk.Frame(self.root)
        self.header.pack(fill=tk.X, padx=self.PAD, pady=self.PAD)
        self.back_btn = tk.Button(
            self.header, text='\u2190 \u623b\u308b',
            command=self.go_back, state=tk.DISABLED
        )
        self.back_btn.pack(side=tk.LEFT)
        self.path_label = tk.Label(self.header, text='スキャン中...', anchor='w')
        self.path_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))

        # キャンバス
        self.canvas = tk.Canvas(self.root, bg='#1e1e1e', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=self.PAD, pady=self.PAD)

        # ステータス
        self.status = tk.Label(
            self.root, text='Dドライブをスキャン中...', anchor='w'
        )
        self.status.pack(fill=tk.X, side=tk.BOTTOM, padx=self.PAD, pady=(0, self.PAD))

        # イベント
        self.canvas.bind('<Button-1>', self.on_click)
        self.canvas.bind('<Motion>', self.on_motion)
        self.canvas.bind('<Configure>', self.on_configure)
        self.root.bind('<BackSpace>', lambda _e: self.go_back())
        self.root.bind('<Escape>', lambda _e: self.go_root())

        self.queue: queue.Queue = queue.Queue()
        self._poll_queue()
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self) -> None:
        try:
            root = scan_directory(self.target_path, self._status)
            self.queue.put(root)
        except Exception as e:
            self.queue.put(e)

    def _status(self, msg: str) -> None:
        self.queue.put(msg)

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.queue.get_nowait()
                if isinstance(msg, FolderNode):
                    self.root_node = msg
                    self.current_node = msg
                    self.history = [msg]
                    self._update_header()
                    self.status.config(
                        text='準備完了 - マウスを移動して情報を表示 / クリックでズーム'
                    )
                    self.on_configure()
                elif isinstance(msg, Exception):
                    self.status.config(text=f'エラー: {msg}')
                else:
                    self.status.config(text=str(msg))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def on_configure(self, event: Optional[tk.Event] = None) -> None:
        """ウィンドウサイズ変更後に再描画する（デバウンス付き）。"""
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(200, self.draw)

    def _update_header(self) -> None:
        if not self.current_node:
            return
        text = f'{display_path(self.current_node.path)}  -  {human_readable(self.current_node.size)}'
        self.path_label.config(text=text)
        self.back_btn.config(
            state=tk.NORMAL if len(self.history) > 1 else tk.DISABLED
        )

    def draw(self) -> None:
        if not self.current_node:
            return

        self.canvas.delete('all')
        self.rectangles = []

        cw = max(1, self.canvas.winfo_width() - self.PAD * 2)
        ch = max(1, self.canvas.winfo_height() - self.PAD * 2)
        if cw < 1 or ch < 1:
            return

        children = [c for c in self.current_node.children if c.size > 0]
        if not children:
            self.canvas.create_text(
                cw / 2 + self.PAD, ch / 2 + self.PAD,
                text='サブフォルダがありません', fill='white', font=self.font
            )
            return

        values = [float(c.size) for c in children]
        rects = squarify(values, self.PAD, self.PAD, cw, ch)

        for i, (x, y, w, h) in enumerate(rects):
            node = children[i]
            if w < self.MIN_RECT_SIZE or h < self.MIN_RECT_SIZE:
                continue
            color = self._color(node)
            self.canvas.create_rectangle(
                x, y, x + w, y + h, fill=color, outline='#333333', width=1
            )
            self.rectangles.append((x, y, x + w, y + h, node))
            self._draw_label(node, x, y, w, h)

    def _color(self, node: FolderNode) -> str:
        """フォルダ名の CRC と相対サイズで色を決定する。"""
        crc = zlib.crc32(node.name.encode('utf-8', 'replace')) & 0xffffffff
        hue = (crc % 360) / 360.0
        parent_size = self.current_node.size if self.current_node and self.current_node.size else 1
        ratio = min(1.0, float(node.size) / float(parent_size))
        value = 0.75 + ratio * 0.18
        r, g, b = colorsys.hsv_to_rgb(hue, 0.60, value)
        return f'#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}'

    def _draw_label(self, node: FolderNode, x: float, y: float, w: float, h: float) -> None:
        """矩形の中に収まる場合はフォルダ名と容量を描画する。"""
        if w < 24 or h < 24:
            return

        size_text = human_readable(node.size)
        max_w = max(self.font.measure(node.name), self.font.measure(size_text))
        line_h = self.font.metrics('linespace')
        total_h = line_h * 2

        if max_w < w - 8 and total_h < h - 8:
            self.canvas.create_text(
                x + w / 2, y + h / 2,
                text=f'{node.name}\n{size_text}',
                fill='white', font=self.font, justify=tk.CENTER, anchor='center'
            )
        elif self.small_font.measure(node.name) < w - 6 and self.small_font.metrics('linespace') < h - 6:
            self.canvas.create_text(
                x + w / 2, y + h / 2,
                text=node.name,
                fill='white', font=self.small_font, anchor='center'
            )

    def _find_node(self, px: int, py: int) -> Optional[FolderNode]:
        # 後ろから検索し、小さい矩形を優先する
        for x1, y1, x2, y2, node in reversed(self.rectangles):
            if x1 <= px <= x2 and y1 <= py <= y2:
                return node
        return None

    def on_click(self, event: tk.Event) -> None:
        node = self._find_node(event.x, event.y)
        if not node:
            return
        if node.children:
            self.history.append(node)
            self.current_node = node
            self._update_header()
            self.draw()
        else:
            self.status.config(
                text=f'{display_path(node.path)} : {human_readable(node.size)} (サブフォルダなし)'
            )

    def on_motion(self, event: tk.Event) -> None:
        node = self._find_node(event.x, event.y)
        if node:
            self.status.config(
                text=f'{display_path(node.path)} : {human_readable(node.size)}'
            )
        else:
            self.status.config(text='クリックでズーム / BackSpaceで戻る')

    def go_back(self) -> None:
        if len(self.history) > 1:
            self.history.pop()
            self.current_node = self.history[-1]
            self._update_header()
            self.draw()

    def go_root(self) -> None:
        if self.root_node:
            self.history = [self.root_node]
            self.current_node = self.root_node
            self._update_header()
            self.draw()


def get_default_path() -> str:
    if sys.platform == 'win32':
        return 'D:\\'
    return os.getcwd()


def main() -> None:
    parser = argparse.ArgumentParser(description='フォルダ容量をツリーマップで可視化')
    parser.add_argument('--path', default=get_default_path(), help='対象フォルダ')
    args = parser.parse_args()

    root = tk.Tk()
    TreemapApp(root, args.path)
    root.mainloop()


if __name__ == '__main__':
    main()
