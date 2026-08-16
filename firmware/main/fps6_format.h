/*
 * fps6_format.h — FPS6 帧格式常量（固件唯一定义处）
 *
 * 数值以协议文档为准：docs/protocol/fps6-format.md（帧格式唯一人写权威）。
 * 服务端打包见 services/app/epd_image.py。修改帧契约时先改协议文档，
 * 再同步此处与 Python 侧；display（分区偏移）与 content_client（大小校验）
 * 一律引用本头文件，不得手写字面量。
 */
#pragma once

#define FPS6_MAGIC       "FPS6"   /* 帧头魔数，4 字节（偏移 0） */
#define FPS6_HEADER_SIZE 20       /* 帧头：magic|width u32LE|height u32LE|format|palette_version|reserved */
#define FPS6_DATA_SIZE   960000   /* 数据区：1200*1600/2 字节（4bit/px 双 IC 布局） */
#define FPS6_FRAME_SIZE  (FPS6_HEADER_SIZE + FPS6_DATA_SIZE) /* 整帧 = 头 + 数据区 */
