xizhi_api_url = 'https://xizhi.qqoq.net/XZ73978bf4e60bf30ffebd41793809c5e8.send'
config_list = [
    # 出成绩截图
    {
        "out": '有成绩',
        "wait_time_sec": 100,
        "hit_config": {},
        "search_img_list": [
            # {
            #     "img_name": "jipo.png",
            #     "region": [0, 0, 900, 360],
            #     "confidence": 0.8
            # },
            {
                "img_name": "sure_btn",
                "region": [28, 673, 800, 1080],
                "confidence": 0.8
            },
            {
                "img_name": "huajie",
                "region": [28, 540, 800, 1080],
                "confidence": 0.8
            }
        ],
        "send_msg": {
            "enable": True,
            "title": " 有个成绩！"
        }
    },
    # 出新成绩截图
    {
        "out": '新成绩',
        "wait_time_sec": 100,
        "hit_config": {},
        "search_img_list": [
            # {
            #     "img_name": "jipo.png",
            #     "region": [0, 0, 900, 360],
            #     "confidence": 0.8
            # },
            {
                "img_name": "sure_btn",
                "region": [28, 673, 800, 1080],
                "confidence": 0.8
            },
            {
                "img_name": "huajie",
                "region": [28, 540, 800, 1080],
                "confidence": 0.8
            },
            {
                "img_name": "time_new",
                "region": [0, 100, 900, 600],
                "confidence": 0.75
            }
        ],
        "send_msg": {
            "enable": True,
            "title": " 打出了新成绩！"
        }
    },
    # 单boss截图，看流程
    {
        "out": '单boss击破',
        "wait_time_sec": 30,
        "hit_config": {},
        "search_img_list": [
            {
                "img_name": "single",
                "region": [960, 100, 1920, 1080],
                "confidence": 0.8
            }
        ]
    },
]