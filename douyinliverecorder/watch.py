import os
import sys
import time
import uuid
import shutil
import pyautogui as auto
import io
import base64

from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pyautogui import locate
from msg_push import xizhi
from urllib.parse import quote
from PIL import Image
from wq import config_list
from wq import xizhi_api_url

script_path = os.path.split(os.path.realpath(sys.argv[0]))[0]

temp_dir_path = f'{script_path}/downloads/temp'

if os.path.exists(temp_dir_path):
    shutil.rmtree(temp_dir_path)
if not os.path.exists(temp_dir_path):
    os.makedirs(temp_dir_path)


def locate_0(search_img: str, big_img: str, region, confidence):
    try:
        ret_val = locate(search_img, big_img, region=region, confidence=confidence)
        if ret_val:
            return True
    except auto.ImageNotFoundException:
        return False


def test():
    # 测试
    search_img = {
        "img_name": "time_new",
        "region": [0, 100, 900, 600],
        "confidence": 0.75
    }
    new_path = 'C:\\Users\\wq\\Downloads\\11.png'
    ret_val = check(search_img, new_path)
    print('ret_val = ' + str(ret_val))

    send_result_msg('星凉（叶子大圣）', 'C:\\Users\\wq\\Downloads\\11.png')


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
        print(f'发送微信消息时报错: {e}')


def check(search_img, new_path):
    img_name = search_img["img_name"]
    region = search_img["region"]
    confidence = search_img["confidence"]
    # 遍历目录
    has_any_right = False
    directory = Path(f'{script_path}/search/{img_name}')
    for file_path in directory.rglob('*'):
        if file_path.is_file():
            ret_val = locate_0(str(file_path), new_path, region, confidence)
            if ret_val:
                has_any_right = True
                break
    return has_any_right


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
        global hit_timestamp
        if event.is_directory:
            return None
        elif event.event_type == 'created':
            try:
                # 当文件被创建时
                # print(f"Received created event - {event.src_path}.")

                # 先休息一会,等待图片完全输出
                time.sleep(0.5)

                # 移动文件并重命名，目录彻底英文化
                old_path = event.src_path
                old_name = os.path.basename(old_path)
                new_name = str(time.time()) + '-' + str(uuid.uuid4()) + '.png'
                new_path = os.path.join(temp_dir_path, new_name)
                os.rename(old_path, new_path)

                for config in config_list:
                    out = config["out"]
                    author_name = self.anchor_name
                    wait_time_sec = config["wait_time_sec"]
                    hit_config = config["hit_config"]
                    search_img_list = config["search_img_list"]
                    send_msg = config.get("send_msg", {})
                    send_msg_enable = send_msg.get("enable", False)
                    send_msg_title = send_msg.get("title", " 监控到新截图")

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
                        print('识别到指定截图！！ ' + old_path)
                        hit_config[author_name] = time.time()

                        if send_msg_enable:
                            send_result_msg(author_name, new_path, send_msg_title)

                        # 复制图片
                        now_time_str = time.strftime('%Y-%m-%d')
                        out_path = f'{script_path}/downloads/{out}/{now_time_str}/{author_name}'

                        if not os.path.exists(out_path):
                            os.makedirs(out_path)

                        destination_path = os.path.join(out_path, old_name)
                        time.sleep(0.1)
                        # 拷贝单个文件
                        shutil.copy2(new_path, destination_path)

                # 最后删除图片
                time.sleep(0.5)
                os.remove(new_path)

            except Exception as e:
                print(f'监控文件创建-挨个处理时报错: {e}')
                # logger.error(f'监控文件创建-挨个处理时报错: {e}')
