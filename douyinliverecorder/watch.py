import base64
import io
import os
import shutil
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import pyautogui as auto
from PIL import Image
from pyautogui import locate
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from douyinliverecorder.utils import logger
from msg_push import xizhi
from wq import config_list
from wq import xizhi_api_url

script_path = os.path.split(os.path.realpath(sys.argv[0]))[0]
temp_dir_path = f'{script_path}/downloads/2_temp'

# 文件创建时间
file_status = {}
hit_timestamp_config = {}

file_handle_executor = ThreadPoolExecutor(max_workers=3)
hit_config_lock = threading.RLock()


def locate_0(search_img: str, big_img: str, region, confidence, print_error: bool = False):
    try:
        ret_val = locate(search_img, big_img, region=region, confidence=confidence)
        if ret_val:
            return True
    except auto.ImageNotFoundException as e:
        if print_error:
            print('图片不匹配，结果如下')
            traceback.print_exc()
        return False


def test():
    # 测试
    search_img = {
        "img_name": "time_new",
        "region": [0, 100, 900, 600],
        "confidence": 0.75
    }
    # search_img = {
    #     "img_name": "sure_btn",
    #     "region": [28, 673, 800, 1080],
    #     "confidence": 0.8
    # }
    # search_img = {
    #     "img_name": "huajie",
    #     "region": [28, 540, 800, 1080],
    #     "confidence": 0.8
    # }
    new_path = 'C:\\Users\\wq\\Downloads\\11.png'
    ret_val = check(search_img, new_path, True)
    print('ret_val = ' + str(ret_val))

    # send_result_msg('星凉（叶子大圣）', 'C:\\Users\\wq\\Downloads\\11.png','有成绩')


def send_result_msg(author_name, img_path, title):
    try:
        img = Image.open(img_path)
        buffer = io.BytesIO()
        img = img.convert('RGB')
        # 获取图片的宽度和高度
        width, height = img.size
        # 计算左半边的宽度（这里是原图宽度的一半）
        left_half_width = width // 2
        # 定义左半边的区域
        left_half_region = (0, 0, left_half_width, height)
        # 剪切左半边
        left_half_img = img.crop(left_half_region)
        left_half_img.save(buffer, format='JPEG', quality=20)  # 压缩图片并保存到字节流中
        img_bytes = buffer.getvalue()
        img_base64 = base64.b64encode(img_bytes).decode('gbk')
        img_data = quote('data:image/png;base64,' + img_base64)
        xizhi(xizhi_api_url, author_name + title, '成绩截图：  \n![成绩截图](' + img_data + ')')
    except Exception as e:
        logger.error(f'发送微信消息时报错: {e}')


def check(search_img, new_path, print_error: bool = False):
    img_name = search_img["img_name"]
    region = search_img["region"]
    confidence = search_img["confidence"]
    # 遍历目录
    has_any_right = False
    directory = Path(f'{script_path}/search/{img_name}')
    for file_path in directory.rglob('*'):
        if file_path.is_file():
            ret_val = locate_0(str(file_path), new_path, region, confidence, print_error)
            if ret_val:
                has_any_right = True
                break
    return has_any_right


def check_file(new_path, author_name, old_name):
    try:
        for config in config_list:
            try:
                hit_config_lock.acquire()
                out = config["out"]
                wait_time_sec = config["wait_time_sec"]
                search_img_list = config["search_img_list"]
                send_msg = config.get("send_msg", {})
                send_msg_enable = send_msg.get("enable", False)
                send_msg_title = send_msg.get("title", " 监控到新截图")

                hit_config = hit_timestamp_config.get(out, {})
                hit_timestamp = hit_config.get(author_name, 0)
                time_pass = time.time() - hit_timestamp
                if hit_timestamp != 0 and time_pass < wait_time_sec:
                    # print('之前已有截图，等待中，此截图跳过！')
                    continue

                all_right = True
                for search_img in search_img_list:
                    has_any_right = check(search_img, new_path)
                    if not has_any_right:
                        all_right = False
                        break

                if all_right:
                    hit_config[author_name] = time.time()
                    hit_timestamp_config[out] = hit_config

                    if send_msg_enable:
                        send_result_msg(author_name, new_path, send_msg_title)

                    # 复制图片
                    now_time_str = time.strftime('%Y-%m-%d')
                    out_path = f'{script_path}/downloads/{out}/{now_time_str}/{author_name}'

                    if not os.path.exists(out_path):
                        os.makedirs(out_path)

                    destination_path = os.path.join(out_path, old_name)
                    # 拷贝单个文件
                    shutil.copy(new_path, destination_path)
            except Exception as e2:
                exc_string = traceback.format_exc()
                logger.error(f'check_file 内循环报错: {e2}\n' + exc_string + '\n\n\n')
            finally:
                hit_config_lock.release()

    except Exception as e:
        exc_string = traceback.format_exc()
        logger.error(f'check_file 报错: {e}\n' + exc_string + '\n\n\n')
    finally:
        # 最后删除图片
        while True:
            try:
                os.remove(new_path)
                break
            except Exception as e:
                logger.error(f'remove file 报错: {e} file = {new_path}\n')
                time.sleep(0.1)


def process_filename(filename):
    # 去除前22位字符
    new_filename = filename[22:]
    # 去除.png后缀
    if new_filename.endswith('.png'):
        new_filename = new_filename[:-4]
    return new_filename


def rename_and_check(png_path, new_path, anchor_name, old_name):
    time.sleep(0.15)
    while True:
        try:
            os.rename(png_path, new_path)
            break
        except Exception as e:
            time.sleep(0.35)
            logger.error(f'重命名失败: {e}')
    check_file(new_path, anchor_name, old_name)


class Watcher:
    def __init__(self, directory_to_watch, anchor_name):
        self.observer = Observer()
        self.directory_to_watch = directory_to_watch
        self.anchor_name = anchor_name

    def run(self):
        event_handler = Handler(self.anchor_name)
        self.observer.schedule(event_handler, self.directory_to_watch, recursive=True)
        self.observer.start()
        try:
            while True:
                time.sleep(1)
        except:
            self.observer.stop()
            print("Observer Stopped")

        self.observer.join()


class Handler(FileSystemEventHandler):
    def __init__(self, anchor_name):
        self.anchor_name = anchor_name

    def on_any_event(self, event):
        png_path = event.src_path
        if event.is_directory:
            return None
        elif event.event_type == 'created':
            try:
                # 提交任务到线程池
                file_status[png_path] = time.time()
            except Exception as e:
                logger.error(f'监控文件创建-挨个处理时报错: {e}')
        elif event.event_type == 'modified':
            try:
                create_time = file_status.get(png_path, 0)
                if create_time > 0:
                    del file_status[png_path]
                    # 移动文件并重命名，目录彻底英文化
                    old_name = os.path.basename(png_path)
                    new_name = str(time.time()) + '-' + str(uuid.uuid4()) + '.png'
                    new_path = os.path.join(temp_dir_path, new_name)
                    anchor_name = process_filename(old_name)

                    file_handle_executor.submit(rename_and_check, png_path, new_path, anchor_name, old_name)


            except Exception as e:
                logger.error(f'on_any_event - modified 时报错: {e}')
