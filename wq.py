xizhi_api_url = 'https://xizhi.qqoq.net/XZ73978bf4e60bf30ffebd41793809c5e8.send'
config_list = [
    # 出新成绩截图
    {
        "out": '新成绩',
        "wait_time_sec": 120,
        "search_img_list": [
            {
                "img_name": "huajie",
                "region": [206, 191, 1102, 907],
                "confidence": 0.70
            },
            {
                "img_name": "time_new",
                "region": [0, 100, 900, 600],
                "confidence": 0.70
            }
        ],
        "send_msg": {
            "enable": True,
            "title": " 打出了新成绩！"
        }
    },
    # 出成绩截图
    {
        "out": '有成绩',
        "wait_time_sec": 120,
        "search_img_list": [
            {
                "img_name": "huajie",
                "region": [206, 191, 1102, 907],
                "confidence": 0.70
            },
        ],
        "send_msg": {
            "enable": False,
            "title": " 有个成绩！"
        }
    },
    # 单boss截图，看流程
    # {
    #     "out": '单boss击破',
    #     "wait_time_sec": 30,
    #     "search_img_list": [
    #         {
    #             "img_name": "single",
    #             "region": [960, 100, 1920, 1080],
    #             "confidence": 0.8
    #         }
    #     ]
    # },
]

spec_config = {
    "wait_time_sec": 120,
    "new_send_msg": {
        "enable": True,
        "title": " 打出了新成绩！"
    },
    "has_send_msg": {
        "enable": False,
        "title": " 有个成绩！"
    },
    "has": {
        "img_name": "huajie",
        "region": [206, 191, 1102, 907],
        "confidence": 0.70
    },
    "new": {
        "img_name": "time_new",
        "region": [0, 100, 900, 600],
        "confidence": 0.70
    },
    "right_game": {
        "img_name": "single",
        "region": [960, 100, 1920, 1080],
        "confidence": 0.8
    }
}
