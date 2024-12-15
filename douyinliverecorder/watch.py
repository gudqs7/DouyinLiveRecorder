import os
import sys
import time
import uuid

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pyautogui import locate
import pyautogui as auto
from douyinliverecorder.utils import logger

script_path = os.path.split(os.path.realpath(sys.argv[0]))[0]

out_right_path = f'{script_path}/downloads/results'
temp_dir_path = f'{script_path}/downloads/temp'

# need_image = f'{script_path}/search/jipo.png'
# need_image_region = [312, 115, 500, 246]

need_image = f'{script_path}/search/nandu.png'
need_image_region = [1240, 422, 1606, 697]

# 跳过 5分钟
# wait_time_sec = 60 * 5
wait_time_sec = 30
hit_timestamp = time.time() - wait_time_sec - 1

if not os.path.exists(temp_dir_path):
    os.makedirs(temp_dir_path)
if not os.path.exists(out_right_path):
    os.makedirs(out_right_path)


def locate_0(search_img: str, big_img: str, region, confidence):
    try:
        ret_val = locate(search_img, big_img, region=region, confidence=confidence)
        if ret_val:
            return True
    except auto.ImageNotFoundException:
        return False


class Watcher:
    def __init__(self, directory_to_watch, anchor_name):
        self.observer = Observer()
        self.directory_to_watch = directory_to_watch
        self.anchor_name = anchor_name

    def run(self):
        event_handler = Handler()
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
    @staticmethod
    def on_any_event(event):
        global hit_timestamp
        if event.is_directory:
            return None
        elif event.event_type == 'created':
            try:
                # 当文件被创建时
                # print(f"Received created event - {event.src_path}.")

                time_pass = time.time() - hit_timestamp
                if time_pass < wait_time_sec:
                    print('之前已有截图，等待中，此截图跳过！')
                    os.remove(event.src_path)
                    return None

                # 先休息一会
                time.sleep(0.5)

                old_path = event.src_path
                old_name = os.path.basename(old_path)

                new_name = str(time.time()) + '-' + str(uuid.uuid4()) + '.png'

                new_path = os.path.join(temp_dir_path, new_name)
                os.rename(old_path, new_path)

                ret_val = locate_0(need_image, new_path, need_image_region, 0.8)
                if ret_val:
                    print('识别到指定截图！！ ' + old_path)
                    hit_timestamp = time.time()

                    # 移动图片
                    old_dir = os.path.dirname(old_path).replace('/', os.path.sep)
                    down_path = os.path.join(script_path, 'downloads').replace('/', os.path.sep)
                    author_path = old_dir.replace(down_path, '')
                    out_path = f'{out_right_path}/{author_path}'
                    out_path = out_path.replace(os.path.sep + 'pngs', '')
                    if not os.path.exists(out_path):
                        os.makedirs(out_path)

                    destination_path = os.path.join(out_path, old_name)
                    time.sleep(0.2)
                    os.rename(new_path, destination_path)
                else:
                    # 删除图片
                    os.remove(new_path)
            except Exception as e:
                logger.error(f'监控文件创建-挨个处理时报错: {e}')
