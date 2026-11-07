# -*- coding: utf-8 -*-
"""子进程：模拟 Ink log-update 的 3 帧，每帧包在 BSU/ESU 里。"""
import sys, time

BSU = "\x1b[?2026h"
ESU = "\x1b[?2026l"
ERASE = "\x1b[2K"

for frame in range(3):
    body = (ERASE + "FRAME-%d-LINE-A\r\n" % frame) * 3
    sys.stdout.write(BSU + body + ESU)
    sys.stdout.flush()
    time.sleep(0.15)

sys.stdout.write("ALLDONE\r\n")
sys.stdout.flush()
time.sleep(0.5)
