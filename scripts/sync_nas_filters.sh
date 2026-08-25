#!/usr/bin/env bash
# sync_nas_filters.sh — rsync 白名单：目录可递归，只有图片文件放行。
#
# 使用字符类匹配大小写扩展名：NAS 上常见的 .JPG / .PNG 不能被漏掉。
# 非图片内容由最后的 --exclude='*' 排除；不要按目录名排除可能包含照片的目录。

EXCLUDES=(
  --exclude='@eaDir' --exclude='#recycle' --exclude='.DS_Store' --exclude='Thumbs.db'
  --exclude='N8BookData/'
  --include='*/'
  --include='*.[jJ][pP][gG]' --include='*.[jJ][pP][eE][gG]'
  --include='*.[pP][nN][gG]' --include='*.[gG][iI][fF]'
  --include='*.[hH][eE][iI][cC]' --include='*.[hH][eE][iI][fF]'
  --include='*.[wW][eE][bB][pP]' --include='*.[bB][mM][pP]'
  --include='*.[tT][iI][fF]' --include='*.[tT][iI][fF][fF]'
  --include='*.[cC][rR]2' --include='*.[nN][eE][fF]'
  --include='*.[aA][rR][wW]' --include='*.[dD][nN][gG]'
  --include='*.[rR][aA][fF]' --include='*.[oO][rR][fF]'
  --include='*.[rR][wW]2' --include='*.[pP][eE][fF]'
  --include='*.[sS][rR][wW]' --include='*.[xX][3][fF]'
  --exclude='*'
)
