from data_collect.utils.notify import ensure_ding_prefix


def test_ensure_ding_prefix_adds_prefix():
    assert ensure_ding_prefix("测试消息").startswith("白白胖胖说")


def test_ensure_ding_prefix_keeps_existing():
    assert ensure_ding_prefix("白白胖胖说：原消息") == "白白胖胖说：原消息"
