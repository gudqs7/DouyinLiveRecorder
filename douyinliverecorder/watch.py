import os
import sys
import time
import uuid
import shutil

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pyautogui import locate
import pyautogui as auto

# from douyinliverecorder.utils import logger

script_path = os.path.split(os.path.realpath(sys.argv[0]))[0]

# out_right_path = f'{script_path}/downloads/results'
temp_dir_path = f'{script_path}/downloads/temp'

config_list = [
    # 出成绩截图
    {
        "out_right_path": f'{script_path}/downloads/有成绩',
        "wait_time_sec": 100,
        "hit_config": {},
        "search_img_list": [
            {
                "img_name": "jipo.png",
                "region": [314, 108, 451, 207],
                "confidence": 0.8
            },
            {
                "img_name": "sure_btn.png",
                "region": [153, 975, 224, 1020],
                "confidence": 0.8
            }
        ]
    },
    # 出新成绩截图
    {
        "out_right_path": f'{script_path}/downloads/新成绩',
        "wait_time_sec": 100,
        "hit_config": {},
        "search_img_list": [
            {
                "img_name": "time_new.png",
                "region": [39, 88, 534, 536],
                "confidence": 0.6
            },
            {
                "img_name": "jipo.png",
                "region": [314, 108, 451, 207],
                "confidence": 0.7
            },
            {
                "img_name": "sure_btn.png",
                "region": [28, 873, 469, 1080],
                "confidence": 0.7
            }
        ]
    },
    # 单boss截图，看流程
    {
        "out_right_path": f'{script_path}/downloads/单boss击破',
        "wait_time_sec": 30,
        "hit_config": {},
        "search_img_list": [
            {
                "img_name": "single.png",
                "region": [1142, 258, 1713, 632],
                "confidence": 0.8
            }
        ]
    },
]

# need_image_region = [312, 115, 500, 246]
# need_image_region = [1240, 422, 1606, 697]

need_image_dir = f'{script_path}/search'

# 跳过 5分钟
# wait_time_sec = 60 * 5
# wait_time_sec = 30
# hit_timestamp = time.time() - wait_time_sec - 1

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
    need_image0 = f'{need_image_dir}/single.png'
    ret_val = locate(need_image0, 'C:\\Users\\wq\\Downloads\\PixPin_2024-12-15_19-58-37.png',
                     region=[1142, 258, 1713, 632], confidence=0.99)
    print('ret_val = ' + str(ret_val))


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
                    out_right_path = config["out_right_path"]
                    wait_time_sec = config["wait_time_sec"]
                    hit_config = config["hit_config"]
                    hit_timestamp = hit_config.get(self.anchor_name, 0)

                    time_pass = time.time() - hit_timestamp
                    if hit_timestamp != 0 and time_pass < wait_time_sec:
                        # print('之前已有截图，等待中，此截图跳过！')
                        continue

                    right = True
                    search_img_list = config["search_img_list"]
                    for search_img in search_img_list:
                        img_name = search_img["img_name"]
                        region = search_img["region"]
                        confidence = search_img["confidence"]
                        need_image = f'{need_image_dir}/{img_name}'

                        ret_val = locate_0(need_image, new_path, region, confidence)
                        if not ret_val:
                            right = False
                            break

                    if right:
                        print('识别到指定截图！！ ' + old_path)
                        hit_config[self.anchor_name] = time.time()

                        # 复制图片
                        old_dir = os.path.dirname(old_path).replace('/', os.path.sep)
                        down_path = os.path.join(script_path, 'downloads').replace('/', os.path.sep)
                        author_path = old_dir.replace(down_path, '')
                        out_path = f'{out_right_path}/{author_path}'
                        out_path = out_path.replace(os.path.sep + 'pngs', '')
                        out_path = out_path.replace(os.path.sep + '抖音直播', '')
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
                print('')
                # logger.error(f'监控文件创建-挨个处理时报错: {e}')
