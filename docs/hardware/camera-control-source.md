# 相机控制交接原文转录

[阅读整理稿](camera-control-handoff.md)

来源：`tmp/相机控制-tmp.xlsx`，工作表 `Sheet1`。本文件保留全部非空单元格的文字、换行、行列位置、5 张图片及删除线；空单元格保持为空，不把合并单元格的文字复制成多条记录。原表蓝色底色按整理稿中的设备和任务分区表达，黄色高亮另行标注。

源文件 SHA-256：`40d9115a50668fad256c68746d4ef1f4bf7d82c2c25a685cf0db6836ccc92b48`。

原表包含 182 个有内容的行、468 个非空单元格；工作表区域为 `A1:R212`，文字和图片位于 A—E 列。未发现隐藏行列、批注、超链接或其他工作表。

删除线的含义尚不明确，所有划线内容均按“原表划线，效力待确认”保留。B131 只有 `MP4 /` 片段被划线，保留其局部格式。图片公式按原式列出，图片从工作簿中原样提取。

合并区域：`B18:D23`、`D129:D130`、`D162:D163`、`D164:D165`。例如 D129 的命令同时对应第 129、130 行描述的分辨率和帧率。

| 行 | A | B | C | D | E |
| --- | --- | --- | --- | --- | --- |
| <a id="row-1"></a>1 | <a id="a1"></a>`9.9会议既要` |  |  |  |  |
| <a id="row-2"></a>2 | <a id="a2"></a>`1.RTC扣掉，所有定时开拍功能去除` | <a id="b2"></a>`rtc扣掉之后，adb录制，删除，确认文件index是否会重复`（原表黄色高亮） |  |  |  |
| <a id="row-3"></a>3 |  | <a id="b3"></a>`测试：` |  |  |  |
| <a id="row-4"></a>4 |  | <a id="b4"></a>`上电，adb录制5个视频，下电` |  |  |  |
| <a id="row-5"></a>5 |  | <a id="b5"></a>`上电，adb删除2个视频，下电` |  |  |  |
| <a id="row-6"></a>6 |  | <a id="b6"></a>`上电，adb录制2个视频，判断文件index` |  |  |  |
| <a id="row-8"></a>8 | <a id="a8"></a>`2.素材叠加index递增，index满只后会生成新文件夹` | <a id="b8"></a>`@hulk补充文件和文件夹逻辑` |  |  |  |
| <a id="row-9"></a>9 | <a id="a9"></a>`3.都是HEVC， H265不可选择` |  |  |  |  |
| <a id="row-11"></a>11 | <a id="a11"></a>`4.码率情况` | <a id="b11"></a>`Action6 8K30 视频高码率130mbps，标准码率95mbps` |  |  |  |
| <a id="row-12"></a>12 |  | <a id="b12"></a>`Action6 4K30延时视频码率：80mbps` |  |  |  |
| <a id="row-13"></a>13 |  | <a id="b13"></a>`Action6 4K30 16:9静止延时 raw jpeg 图片参考4K拍照大小` |  |  |  |
| <a id="row-15"></a>15 |  | <a id="b15"></a>`OSMO360II 8K30视频码率  170mbps（双球）` |  |  |  |
| <a id="row-16"></a>16 |  | <a id="b16"></a>`OSMO360II 8K30静止延时码率 300mbps（双球）` |  |  |  |
| <a id="row-18"></a>18 |  | <a id="b18"></a>`Action 任务` |  |  |  |
| <a id="row-20"></a>20 | <a id="a20"></a>`获取` |  |  |  |  |
| <a id="row-21"></a>21 | <a id="a21"></a>`视频单镜头拍摄` |  |  |  |  |
| <a id="row-22"></a>22 | <a id="a22"></a>`for视频素材，拍摄时长5-20s` |  |  |  |  |
| <a id="row-23"></a>23 | <a id="a23"></a>`在一个指定时间（需要有个和卫星完全同步的时间），设置好相机进入视频拍摄模式，设置好参数` |  |  |  |  |
| <a id="row-24"></a>24 | <a id="a24"></a>`开始录制，T+X秒后结束录制。` |  |  |  |  |
| <a id="row-25"></a>25 |  | <a id="b25"></a>`参考参数设置` |  | <a id="d25"></a>`adb命令提供` |  |
| <a id="row-26"></a>26 |  | <a id="b26"></a>`Action（供电）开机` | <a id="c26"></a>`tx2---相机  上下电，自行硬件控制，不涉及adb` |  |  |
| <a id="row-27"></a>27 |  | <a id="b27"></a>`拍摄模式：Video` | <a id="c27"></a>`切换Action到 视频模式` | <a id="d27"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 0xe1 01` |  |
| <a id="row-28"></a>28 |  | <a id="b28"></a>`分辨率：8K 16:9，7680×4320` | <a id="c28"></a>`设置分辨率8K16:9,30fps` | <a id="d28"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 3703000000` |  |
| <a id="row-29"></a>29 |  | <a id="b29"></a>`帧率：30 fps` | <a id="c29"></a>`设置分辨率4K16:9,30fps` | <a id="d29"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000` |  |
| <a id="row-30"></a>30 |  | <a id="b30"></a>`色彩模式：D-Log M` | <a id="c30"></a>`打开pro模式，` | <a id="d30"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101` |  |
| <a id="row-31"></a>31 |  |  | <a id="c31"></a>`设置色彩为D-Log M` | <a id="d31"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 42  3d` |  |
| <a id="row-32"></a>32 |  | <a id="b32"></a>`FOV：Wide / Natural Wide / Standard，按任务选择` | <a id="c32"></a>`设置视角为Wide ` | <a id="d32"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000101` |  |
| <a id="row-33"></a>33 |  |  | <a id="c33"></a>`设置视角为Natural Wide ` | <a id="d33"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000105` |  |
| <a id="row-34"></a>34 |  |  | <a id="c34"></a>`设置视角为 Standard` | <a id="d34"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000102` |  |
| <a id="row-35"></a>35 |  | <a id="b35"></a>`防抖：off/RockSteady ` | <a id="c35"></a>`设置增稳为off 关闭增稳` | <a id="d35"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010108000100` |  |
| <a id="row-36"></a>36 |  |  | <a id="c36"></a>`设置增稳为RS(Rocksteady)` | <a id="d36"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010108000101` |  |
| <a id="row-37"></a>37 |  | <a id="b37"></a>`切换曝光模式` | <a id="c37"></a>`切换到M档曝光` | <a id="d37"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0400` |  |
| <a id="row-38"></a>38 |  |  | <a id="c38"></a>`切换到Auto档曝光` | <a id="d38"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0100` |  |
| <a id="row-39"></a>39 |  | <a id="b39"></a>`ISO：800` | <a id="c39"></a>`（在M档下）设置IOS 100，` | <a id="d39"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2a 03` |  |
| <a id="row-40"></a>40 |  |  | <a id="c40"></a>`（在M档下）设置IOS 200，` | <a id="d40"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2a 04` |  |
| <a id="row-41"></a>41 |  |  | <a id="c41"></a>`（在M档下）设置IOS 400，` | <a id="d41"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2a 05` |  |
| <a id="row-42"></a>42 |  |  | <a id="c42"></a>`（在M档下）设置IOS 800，` | <a id="d42"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2a 06` |  |
| <a id="row-43"></a>43 |  |  | <a id="c43"></a>`（在M档下）设置IOS 1600，` | <a id="d43"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2a 07` |  |
| <a id="row-44"></a>44 |  |  | <a id="c44"></a>`（在M档下）设置IOS 3200，` | <a id="d44"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2a 08` |  |
| <a id="row-45"></a>45 |  | <a id="b45"></a>`快门：1/60 s` | <a id="c45"></a>`（在M档下）设置 快门1/60s` | <a id="d45"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 28 013C8000` |  |
| <a id="row-46"></a>46 |  | <a id="b46"></a>`白平衡：5200 K` | <a id="c46"></a>`设置白平衡M档，设置手动色温数值5200K` | <a id="d46"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000` |  |
| <a id="row-47"></a>47 |  | <a id="b47"></a>`曝光补偿：+0 EV；若 ISO 和快门为手动锁定，则 EV 仅作为目标值或记录项` | <a id="c47"></a>`设置Auto档位下EV  +0ev` | <a id="d47"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2e 10` |  |
| <a id="row-48"></a>48 |  |  | <a id="c48"></a>`设置Auto档位下EV  -0.7ev` | <a id="d48"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2e 0e` |  |
| <a id="row-49"></a>49 |  |  | <a id="c49"></a>`设置Auto档位下EV +0.7ev` | <a id="d49"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2e 12` |  |
| <a id="row-50"></a>50 |  | <a id="b50"></a>`光圈：可切换 f/2.8 / f/4.0` | <a id="c50"></a>`设置光圈为F2.8` | <a id="d50"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x26 1801` |  |
| <a id="row-51"></a>51 |  |  | <a id="c51"></a>`设置光圈为F4.0` | <a id="d51"></a>~~`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x26 9001`~~ |  |
| <a id="row-52"></a>52 |  | <a id="b52"></a>`切换码率：标标准码率高码率` | <a id="c52"></a>`设置码率标准码率，高码率` | <a id="d52"></a>~~`simulate_device -s bitrate 1  #标准码率`<br>`simulate_device -s bitrate 2  #高码率`~~ |  |
| <a id="row-53"></a>53 |  | <a id="b53"></a>`开始录像` | <a id="c53"></a>`开始录像` | <a id="d53"></a>~~`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 01`~~ |  |
| <a id="row-54"></a>54 |  | <a id="b54"></a>`T0 + 8s：停止录制` | <a id="c54"></a>`停止录像（录制时间由外部脚本控制）` | <a id="d54"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 00` |  |
| <a id="row-55"></a>55 |  | <a id="b55"></a>`T0 + 结束确认：回传状态，记录文件名、大小、时长、时间戳、轨道位置、姿态、Pan / Tilt /Roll、FOV、曝光参数。` | <a id="c55"></a>`获取上次执行的录像文件夹Index和文件index列表，按index排序` | <a id="d55"></a>`检查/mnt/media_rw/emulated/DCIM/ 目录及子目录下生成的新文件，使用adb pull 命令获取`<br>`使用SD卡存储时目录为/mnt/media_rw/sd/DCIM/ ` |  |
| <a id="row-56"></a>56 |  |  | <a id="c56"></a>`获取最新的index文件` |  |  |
| <a id="row-57"></a>57 |  |  | <a id="c57"></a>`文件meta信息包含参数` |  |  |
| <a id="row-59"></a>59 | <a id="a59"></a>`在未来90min（或者更长的时间），相机每隔X秒拍摄一张照片` |  |  |  |  |
| <a id="row-60"></a>60 | <a id="a60"></a>`（有一种可能需要规避，就是可能相机` |  |  |  |  |
| <a id="row-61"></a>61 | <a id="a61"></a>`会设置10秒长曝光，` |  |  |  |  |
| <a id="row-62"></a>62 | <a id="a62"></a>`那拍摄间隔就需要大于曝光时间），在拍摄240张（or更多）后结束拍摄` |  |  |  |  |
| <a id="row-63"></a>63 |  | <a id="b63"></a>`Action` | <a id="c63"></a>`Action（供电）开机` |  |  |
| <a id="row-64"></a>64 | <a id="a64"></a>`1` | <a id="b64"></a>`在指定时间 T0，相机进入 Timelapse 模式。` |  |  |  |
| <a id="row-65"></a>65 | <a id="a65"></a>`2` | <a id="b65"></a>`拍摄模式：Timelapse` | <a id="c65"></a>`切换到timelapse静态延时模式` | <a id="d65"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02` |  |
| <a id="row-66"></a>66 | <a id="a66"></a>`3` | <a id="b66"></a>`输出分辨率：4K` | <a id="c66"></a>`4K30` | <a id="d66"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000` |  |
| <a id="row-67"></a>67 | <a id="a67"></a>`4` | <a id="b67"></a>`输出帧率：30 fps` |  | <a id="d67"></a>`3、4同一条命令` |  |
| <a id="row-68"></a>68 | <a id="a68"></a>`5` | <a id="b68"></a>`拍摄间隔：30s` | <a id="c68"></a>`设置间隔时间30秒` | <a id="d68"></a>`5、6同一条命令` |  |
| <a id="row-69"></a>69 | <a id="a69"></a>`6` | <a id="b69"></a>`持续时间：90分钟` | <a id="c69"></a>`设置持续时间90min` | <a id="d69"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400002c01181500000000000000000000`<br> |  |
| <a id="row-70"></a>70 |  |  | <a id="c70"></a>`切换到Pro模式` | <a id="d70"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101` |  |
| <a id="row-71"></a>71 |  |  | <a id="c71"></a>`切换到M档曝光` | <a id="d71"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0400` |  |
| <a id="row-72"></a>72 | <a id="a72"></a>`8` | <a id="b72"></a>`ISO：800` | <a id="c72"></a>`（在M档下）设置IOS 800` | <a id="d72"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2a 06` |  |
| <a id="row-73"></a>73 | <a id="a73"></a>`9` | <a id="b73"></a>`快门：1/60 s` | <a id="c73"></a>`（在M档下）设置 快门1/60s` | <a id="d73"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 28 013C8000` |  |
| <a id="row-74"></a>74 | <a id="a74"></a>`10` | <a id="b74"></a>`白平衡：5200 K` | <a id="c74"></a>`设置白平衡 5200K` | <a id="d74"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000` |  |
| <a id="row-75"></a>75 | <a id="a75"></a>`11` | <a id="b75"></a>~~`色彩模式：普通视频色彩`~~ | <a id="c75"></a>~~`设置普通视频色彩`~~ | <a id="d75"></a>`默认普通色彩，不支持` |  |
| <a id="row-76"></a>76 | <a id="a76"></a>`13` | <a id="b76"></a>`是否保存原始照片：开启，便于后期重建或单帧筛选` | <a id="c76"></a>`设置video+raw` | <a id="d76"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400032c01181500000000000000000000` |  |
| <a id="row-77"></a>77 |  |  | <a id="c77"></a>`设置video+jpeg，` | <a id="d77"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400022c01181500000000000000000000` |  |
| <a id="row-78"></a>78 | <a id="a78"></a>`14` | <a id="b78"></a>`执行流程：` |  |  |  |
| <a id="row-79"></a>79 | <a id="a79"></a>`15` | <a id="b79"></a>`T0 - 预热时间：开机 / 唤醒相机` |  |  |  |
| <a id="row-80"></a>80 | <a id="a80"></a>`16` | <a id="b80"></a>`T0：开始延时摄影` | <a id="c80"></a>`开始延时摄影` | <a id="d80"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01` |  |
| <a id="row-81"></a>81 | <a id="a81"></a>`17` | <a id="b81"></a>`每 30s 采集一帧` |  |  |  |
| <a id="row-82"></a>82 | <a id="a82"></a>`18` | <a id="b82"></a>`达到任务时长或收到停止指令后结束` | <a id="c82"></a>`获取上次执行的录像文件，和raw文件` | <a id="d82"></a>`检查/mnt/media_rw/emulated/DCIM/ 目录及子目录下生成的新文件，使用adb pull 命令获取`<br>`使用SD卡存储时目录为/mnt/media_rw/sd/DCIM/ ` |  |
| <a id="row-83"></a>83 |  | <a id="b83"></a>`生成延时视频，并` | <a id="c83"></a>`文件meta信息包含参数` |  |  |
| <a id="row-84"></a>84 |  | <a id="b84"></a>`保留原始照片序列。` |  |  |  |
| <a id="row-87"></a>87 | <a id="a87"></a>![A87 原图](assets/camera-control/action-timelapse-100min.jpeg)<br>`=_xlfn.DISPIMG("ID_24B82219C49E49CEAB8EC4F165215A1E",1)` | <a id="b87"></a>`Action6` | <a id="c87"></a>`Action6（供电）开机` |  |  |
| <a id="row-88"></a>88 |  | <a id="b88"></a>`在指定时间 T0，开机` |  |  |  |
| <a id="row-89"></a>89 |  | <a id="b89"></a>`拍摄模式：Timelapse` | <a id="c89"></a>`进入timelapse，静止延时` | <a id="d89"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02` |  |
| <a id="row-90"></a>90 |  | <a id="b90"></a>`输出分辨率：4K` | <a id="c90"></a>`设置分辨率4K` | <a id="d90"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000` |  |
| <a id="row-91"></a>91 |  | <a id="b91"></a>`输出帧率：30 fps` | <a id="c91"></a>`设置帧率30 fps` | <a id="d91"></a>`上一条命令已设置` |  |
| <a id="row-92"></a>92 |  | <a id="b92"></a>`拍摄间隔：25s` | <a id="c92"></a>`拍摄间隔25s` | <a id="d92"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 040000fa00701700000000000000000000` |  |
| <a id="row-93"></a>93 |  | <a id="b93"></a>`持续时间：100分钟` | <a id="c93"></a>`拍摄总时长100分钟` | <a id="d93"></a>`上一条命令已设置` |  |
| <a id="row-94"></a>94 |  | <a id="b94"></a>`设置曝光auto` | <a id="c94"></a>`设置曝光auto（iso range 默认）` | <a id="d94"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100` |  |
| <a id="row-95"></a>95 |  | <a id="b95"></a>`设置延时拍摄文件video+raw` | <a id="c95"></a>`设置延时video+raw` | <a id="d95"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 040003fa00701700000000000000000000` |  |
| <a id="row-96"></a>96 |  |  | <a id="c96"></a>`设置延时video+jpeg` | <a id="d96"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 040002fa00701700000000000000000000` |  |
| <a id="row-97"></a>97 |  |  | <a id="c97"></a>`设置延时video` | <a id="d97"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 040000fa00701700000000000000000000` |  |
| <a id="row-98"></a>98 |  | <a id="b98"></a>`开始延时摄影` | <a id="c98"></a>`开始延时摄影` | <a id="d98"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01` |  |
| <a id="row-101"></a>101 | <a id="a101"></a>![A101 原图](assets/camera-control/action-timelapse-30min.jpeg)<br>`=_xlfn.DISPIMG("ID_48781444FDE04C49AA1CB60B9F286421",1)` | <a id="b101"></a>`Action6` | <a id="c101"></a>`Action6（供电）开机` |  |  |
| <a id="row-102"></a>102 |  | <a id="b102"></a>`在指定时间 T0，开机` |  |  |  |
| <a id="row-103"></a>103 |  | <a id="b103"></a>`拍摄模式：Timelapse` | <a id="c103"></a>`进入timelapse，静止延时` | <a id="d103"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02` |  |
| <a id="row-104"></a>104 |  | <a id="b104"></a>`输出分辨率：4K` | <a id="c104"></a>`设置分辨率4K` | <a id="d104"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000` |  |
| <a id="row-105"></a>105 |  | <a id="b105"></a>`输出帧率：30 fps` | <a id="c105"></a>`设置帧率30 fps` | <a id="d105"></a>`上一条命令已设置` |  |
| <a id="row-106"></a>106 |  | <a id="b106"></a>`拍摄间隔：8s` | <a id="c106"></a>`拍摄间隔8s` | <a id="d106"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400005000080700000000000000000000` |  |
| <a id="row-107"></a>107 |  | <a id="b107"></a>`持续时间：30分钟` | <a id="c107"></a>`拍摄总时长30分钟` | <a id="d107"></a>`上一条命令已设置` |  |
| <a id="row-108"></a>108 |  | <a id="b108"></a>`设置曝光auto` | <a id="c108"></a>`设置曝光auto（iso range 默认）` | <a id="d108"></a>`dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100` |  |
| <a id="row-109"></a>109 |  | <a id="b109"></a>`设置延时拍摄文件video+raw` | <a id="c109"></a>`设置延时video+raw` | <a id="d109"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400035000080700000000000000000000` |  |
| <a id="row-110"></a>110 |  |  | <a id="c110"></a>`设置延时video+jpeg` | <a id="d110"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400025000080700000000000000000000` |  |
| <a id="row-111"></a>111 |  |  | <a id="c111"></a>`设置延时video` | <a id="d111"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400005000080700000000000000000000` |  |
| <a id="row-112"></a>112 |  | <a id="b112"></a>`开始延时摄影` | <a id="c112"></a>`开始延时摄影` | <a id="d112"></a>`dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01` |  |
| <a id="row-120"></a>120 |  |  | <a id="c120"></a>`OSMO 360任务` |  |  |
| <a id="row-123"></a>123 | <a id="a123"></a>`1.2 特殊BTS镜头拍摄` |  |  |  |  |
| <a id="row-124"></a>124 | <a id="a124"></a>`在指定时间，Osmo360II开启拍摄，全景视频模式，延迟3-5s后` |  | <a id="c124"></a>`全景8K30 码率 175M   90Mbps*2` |  |  |
| <a id="row-125"></a>125 | <a id="a125"></a>`action6步进电机向前推，结束拍摄。` |  | <a id="c125"></a>`全景 8k延时 300Mbps，37.5MB/s， 小于10秒` |  |  |
| <a id="row-126"></a>126 |  | <a id="b126"></a>`参考参数设置` | <a id="c126"></a>`adb 指令提供` | <a id="d126"></a>`指令` |  |
| <a id="row-127"></a>127 |  | <a id="b127"></a>`360相机（供电）开机` | <a id="c127"></a>`tx2---相机  上下电` |  |  |
| <a id="row-128"></a>128 | <a id="a128"></a>`1` | <a id="b128"></a>`拍摄模式：Video` | <a id="c128"></a>`切换360到 视频模式` | <a id="d128"></a>`adb shell dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x8e 01013f000101 `<br>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c e1 38 `<br> |  |
| <a id="row-129"></a>129 | <a id="a129"></a>`2` | <a id="b129"></a>`分辨率：8K 2:1  7680 * 3840` | <a id="c129"></a>`设置分辨率8K30` | <a id="d129"></a>`adb shell simulate_device -s BaseFormat 6 69 0` |  |
| <a id="row-130"></a>130 | <a id="a130"></a>`3` | <a id="b130"></a>`帧率：30 fps` | <a id="c130"></a>`设置帧率30fps` |  |  |
| <a id="row-131"></a>131 | <a id="a131"></a>`4` | <a id="b131"></a>`编码格式：`~~`MP4 /`~~` HEVC` | <a id="c131"></a>~~`支持HEVC，不涉及`~~ |  |  |
| <a id="row-132"></a>132 | <a id="a132"></a>`5` | <a id="b132"></a>`色彩模式：D-Log M` | <a id="c132"></a>`打开pro模式，设置色彩为D-Log M` | <a id="d132"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101`<br>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 42 3d` |  |
| <a id="row-133"></a>133 | <a id="a133"></a>`6` | <a id="b133"></a>~~`FOV：Wide / Natural Wide / Standard，按任务选择，全景可以不设`~~ | <a id="c133"></a>~~`设置视角为Wide / Natural Wide / Standard`~~ | <a id="d133"></a><br>`标准：adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000102`<br>`广角：adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000101`<br>`超广角：adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000100` |  |
| <a id="row-134"></a>134 | <a id="a134"></a>`7` | <a id="b134"></a>~~`防抖：RockSteady 或 RockSteady+，8K 下不使用 HorizonSteady`~~ | <a id="c134"></a>~~`设置增稳为Rocksteady （RS+不推荐）`~~ | <a id="d134"></a>`不支持增稳调节` |  |
| <a id="row-135"></a>135 | <a id="a135"></a>`8` |  | <a id="c135"></a>`切换到M档曝光/Auto档` | <a id="d135"></a>`M挡：adb shell  dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 1e 0400`<br>`AUTO挡：adb shell dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 1e 0100` |  |
| <a id="row-136"></a>136 | <a id="a136"></a>`9` | <a id="b136"></a>`ISO：800` | <a id="c136"></a>`（在M档下）设置IOS 800` | <a id="d136"></a>`adb shell  dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 2A 06` |  |
| <a id="row-137"></a>137 | <a id="a137"></a>`10` | <a id="b137"></a>`快门：1/60 s` | <a id="c137"></a>`（在M档下）设置 快门1/60s` | <a id="d137"></a>`adb shell  dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 28 013c8000` |  |
| <a id="row-138"></a>138 | <a id="a138"></a>`11` | <a id="b138"></a>`白平衡：5200 K` | <a id="c138"></a>`设置白平衡 5200K` | <a id="d138"></a>`adb shell  dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2c 0634000000 ` |  |
| <a id="row-139"></a>139 | <a id="a139"></a>`12` | <a id="b139"></a>`曝光补偿：+0 EV；若 ISO 和快门为手动锁定，则 EV 仅作为目标值或记录项` | <a id="c139"></a>`获取M档位下 EV` | <a id="d139"></a>`adb shell  dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2f` |  |
| <a id="row-140"></a>140 | <a id="a140"></a>`13` |  | <a id="c140"></a>`设置Auto档位下EV  +0ev` | <a id="d140"></a>`adb shell  dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2e 10` |  |
| <a id="row-141"></a>141 | <a id="a141"></a>`14` | <a id="b141"></a>~~`光圈：优先 f/2.0；如需控制景深或过曝风险，可切换 f/2.8 / f/4.0`~~ | <a id="c141"></a>~~`无F2.0选项`~~ | <a id="d141"></a>~~`102 不支持 光圈调节`~~ |  |
| <a id="row-142"></a>142 | <a id="a142"></a>`15` |  | <a id="c142"></a>~~`设置光圈为F2.8/F4.0`~~ |  |  |
| <a id="row-143"></a>143 | <a id="a143"></a>`16` | <a id="b143"></a>~~`码率：最高 120 Mbps，实际值待接口确认或实测`~~ | <a id="c143"></a>`设置高码率 175Mbps（不用设，相机可以在触发前配置好）` | <a id="d143"></a>`adb shell  simulate_device -s DeviceRecordRecSettingBitRate 2` |  |
| <a id="row-144"></a>144 | <a id="a144"></a>`17` | <a id="b144"></a>`开始录制` | <a id="c144"></a>`开始录制` | <a id="d144"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 01` |  |
| <a id="row-145"></a>145 | <a id="a145"></a>`18` | <a id="b145"></a>~~`执行流程：`~~ |  |  |  |
| <a id="row-146"></a>146 | <a id="a146"></a>`19` | <a id="b146"></a>~~`T0 - 预热时间：开机 / 唤醒相机`~~ | <a id="c146"></a>~~`设置时间HH:mm:ss启动相机（开机启动）`~~ |  |  |
| <a id="row-147"></a>147 | <a id="a147"></a>`20` | <a id="b147"></a>~~`T0：开始录制`~~ | <a id="c147"></a>~~`设置时间在HH:mm:ss开始以上述设置执行录像 X秒`~~ | <a id="d147"></a>`录制时间由外部脚本控制` |  |
| <a id="row-148"></a>148 | <a id="a148"></a>`21` | <a id="b148"></a>`T0 + 8s：停止录制` | <a id="c148"></a>`停止录像` | <a id="d148"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 00` |  |
| <a id="row-149"></a>149 | <a id="a149"></a>`22` | <a id="b149"></a>`T0 + 结束确认：回传状态，记录文件名、大小、时长、时间戳、轨道位置、姿态、Pan / Tilt /Roll、FOV、曝光参数。` | <a id="c149"></a>`拉取录像文件` | <a id="d149"></a>`adb pull /mnt/media_rw/emulated/DCIM ` |  |
| <a id="row-150"></a>150 | <a id="a150"></a>`23` |  | <a id="c150"></a>`获取上次执行的录像文件` |  |  |
| <a id="row-155"></a>155 | <a id="a155"></a>`在未来90min（或者更长的时间），相机每隔X秒拍摄一张照片` |  |  |  |  |
| <a id="row-156"></a>156 | <a id="a156"></a>`（有一种可能需要规避，就是可能相机` |  |  |  |  |
| <a id="row-157"></a>157 | <a id="a157"></a>`会设置10秒长曝光，` |  |  |  |  |
| <a id="row-158"></a>158 | <a id="a158"></a>`那拍摄间隔就需要大于曝光时间），在拍摄240张（or更多）后结束拍摄` |  |  |  |  |
| <a id="row-159"></a>159 |  | <a id="b159"></a>`OSMO 360 II` | <a id="c159"></a>`OSMO360（供电）开机` | <a id="d159"></a>`指令` |  |
| <a id="row-160"></a>160 | <a id="a160"></a>`1` | <a id="b160"></a>`在指定时间 T0，开机` |  |  |  |
| <a id="row-161"></a>161 | <a id="a161"></a>`2` | <a id="b161"></a>`拍摄模式：Timelapse` | <a id="c161"></a>`进入 全景 timelapse，静止延时` | <a id="d161"></a>`adb shell dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x8e 01013f000101 `<br>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c e1 3b` |  |
| <a id="row-162"></a>162 | <a id="a162"></a>`3` | <a id="b162"></a>`输出分辨率：8K` | <a id="c162"></a>`设置分辨率8K` | <a id="d162"></a>`adb shell  dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 6f03000000` |  |
| <a id="row-163"></a>163 | <a id="a163"></a>`4` | <a id="b163"></a>`输出帧率：30 fps` | <a id="c163"></a>`设置帧率30 fps` |  |  |
| <a id="row-164"></a>164 | <a id="a164"></a>`5` | <a id="b164"></a>`拍摄间隔：30s` | <a id="c164"></a>`拍摄间隔30s` | <a id="d164"></a>`  # 10min`<br>`  adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0407002c015802000000000000000001`<br>`  # 20min`<br>`  adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0407002c01b004000000000000000001`<br>`  # 30min`<br>`  adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0407002c010807000000000000000001`<br>`  # 1H`<br>`  adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0407002c01100e000000000000000001`<br>`  # 2H`<br>`  adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0407002c01201c000000000000000001`<br>`  # 3H`<br>`  adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0407002c01302a000000000000000001`<br>`  # 5H`<br>`  adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0407002c015046000000000000000001` |  |
| <a id="row-165"></a>165 | <a id="a165"></a>`6` | <a id="b165"></a>`持续时间：90分钟` | <a id="c165"></a>`拍摄总时长90 分钟` |  |  |
| <a id="row-166"></a>166 | <a id="a166"></a>`8` | <a id="b166"></a>`ISO：800 ？？？` | <a id="c166"></a>`（在M档下）设置IOS 800` | <a id="d166"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101`<br>`adb shell  dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 1e 0400`<br>`adb shell  dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 2A 06` |  |
| <a id="row-167"></a>167 | <a id="a167"></a>`9` | <a id="b167"></a>`快门：1/60 s` | <a id="c167"></a>`（在M档下）设置 快门1/60s` | <a id="d167"></a>`adb shell  dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 28 013c8000` |  |
| <a id="row-168"></a>168 | <a id="a168"></a>`10` | <a id="b168"></a>`白平衡：5200 K？？？？` | <a id="c168"></a>`设置白平衡 5200K` | <a id="d168"></a>`adb shell  dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2c 0634000000 ` |  |
| <a id="row-169"></a>169 | <a id="a169"></a>`11` | <a id="b169"></a>`色彩模式：普通视频色彩` | <a id="c169"></a>`设置普通视频色彩` | <a id="d169"></a>`不支持设置` |  |
| <a id="row-170"></a>170 | <a id="a170"></a>`12` | <a id="b170"></a>`防抖：Timelapse 模式下不使用 EIS` |  |  |  |
| <a id="row-171"></a>171 | <a id="a171"></a>`13` | <a id="b171"></a>`全景只有video选项` | <a id="c171"></a>`全景延时只有video 175mbps` |  |  |
| <a id="row-172"></a>172 | <a id="a172"></a>`14` | <a id="b172"></a>`执行流程：` |  |  |  |
| <a id="row-173"></a>173 | <a id="a173"></a>`15` | <a id="b173"></a>`T0 - 预热时间：开机 / 唤醒相机` |  |  |  |
| <a id="row-174"></a>174 | <a id="a174"></a>`16` | <a id="b174"></a>`T0：开始延时摄影` | <a id="c174"></a>`开始延时摄影` | <a id="d174"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01` |  |
| <a id="row-175"></a>175 | <a id="a175"></a>`17` | <a id="b175"></a>`每 30s 采集一帧` |  | <a id="d175"></a>`录制时间由外部脚本控制` |  |
| <a id="row-176"></a>176 | <a id="a176"></a>`18` | <a id="b176"></a>`达到任务时长或收到停止指令后结束` | <a id="c176"></a>`停止拍摄` | <a id="d176"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 00` |  |
| <a id="row-177"></a>177 |  | <a id="b177"></a>`生成延时视频，` | <a id="c177"></a>`录像自然结束 获取上次执行的录像文件` | <a id="d177"></a>`adb pull /mnt/media_rw/emulated/DCIM ` |  |
| <a id="row-180"></a>180 | <a id="a180"></a>`Osmo360ii延时任务demo(video175Mbps）:` |  |  | <a id="d180"></a>`adb 指令` |  |
| <a id="row-181"></a>181 | <a id="a181"></a>![A181 原图](assets/camera-control/osmo-timelapse-300min.jpeg)<br>`=_xlfn.DISPIMG("ID_5C01292F85864518B3FEC00CBE01AC17",1)` | <a id="b181"></a>`OSMO 360 II` | <a id="c181"></a>`OSMO360（供电）开机` |  |  |
| <a id="row-182"></a>182 |  | <a id="b182"></a>`在指定时间 T0，开机` |  |  |  |
| <a id="row-183"></a>183 | <a id="a183"></a>`8K30 视频` | <a id="b183"></a>`拍摄模式：Timelapse` | <a id="c183"></a>`进入 全景 timelapse，静止延时` | <a id="d183"></a>`adb shell dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x8e 01013f000101 `<br>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c e1 3b` |  |
| <a id="row-184"></a>184 |  | <a id="b184"></a>`输出分辨率：8K` | <a id="c184"></a>`设置分辨率8K` | <a id="d184"></a>`adb shell  dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 6f03000000` |  |
| <a id="row-185"></a>185 |  | <a id="b185"></a>`输出帧率：30 fps` | <a id="c185"></a>`设置帧率30 fps` |  |  |
| <a id="row-186"></a>186 |  | <a id="b186"></a>`拍摄间隔：40s` | <a id="c186"></a>`拍摄间隔40s` | <a id="d186"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 04000090015046000000000000000000` |  |
| <a id="row-187"></a>187 |  | <a id="b187"></a>`持续时间：300分钟` | <a id="c187"></a>`拍摄总时长300 分钟` |  |  |
| <a id="row-188"></a>188 |  | <a id="b188"></a>`设置曝光auto` | <a id="c188"></a>`设置曝光auto（iso range 默认）` | <a id="d188"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000100`<br>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1e 0000` |  |
| <a id="row-189"></a>189 |  | <a id="b189"></a>`开始延时摄影` | <a id="c189"></a>`开始延时摄影` | <a id="d189"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01` |  |
| <a id="row-192"></a>192 | <a id="a192"></a>![A192 原图](assets/camera-control/osmo-timelapse-100min.jpeg)<br>`=_xlfn.DISPIMG("ID_FADFCE024A7F4D8FA2BFCF1779797518",1)` | <a id="b192"></a>`OSMO 360 II` | <a id="c192"></a>`OSMO360（供电）开机` |  |  |
| <a id="row-193"></a>193 |  | <a id="b193"></a>`在指定时间 T0，开机` |  |  |  |
| <a id="row-194"></a>194 |  | <a id="b194"></a>`拍摄模式：Timelapse` | <a id="c194"></a>`进入 全景 timelapse，静止延时` | <a id="d194"></a>`adb shell dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x8e 01013f000101 `<br>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c e1 3b` |  |
| <a id="row-195"></a>195 | <a id="a195"></a>`8K30 视频` | <a id="b195"></a>`输出分辨率：8K` | <a id="c195"></a>`设置分辨率8K` | <a id="d195"></a>`adb shell  dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 6f03000000` |  |
| <a id="row-196"></a>196 |  | <a id="b196"></a>`输出帧率：30 fps` | <a id="c196"></a>`设置帧率30 fps` |  |  |
| <a id="row-197"></a>197 |  | <a id="b197"></a>`拍摄间隔：25s` | <a id="c197"></a>`拍摄间隔25s` | <a id="d197"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 040000fa00201c000000000000000000` | <a id="e197"></a>`100分钟不支持，修改为2H` |
| <a id="row-198"></a>198 |  | <a id="b198"></a>`持续时间：100分钟` | <a id="c198"></a>`拍摄总时长100分钟` |  |  |
| <a id="row-199"></a>199 |  | <a id="b199"></a>`设置曝光auto` | <a id="c199"></a>`设置曝光auto（iso range 默认）` | <a id="d199"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000100`<br>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1e 0000` |  |
| <a id="row-200"></a>200 |  | <a id="b200"></a>`开始延时摄影` | <a id="c200"></a>`开始延时摄影` | <a id="d200"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01` |  |
| <a id="row-202"></a>202 | <a id="a202"></a>![A202 原图](assets/camera-control/osmo-timelapse-30min.jpeg)<br>`=_xlfn.DISPIMG("ID_975BB07907EF4390A1F5C7C625EA6F4B",1)` | <a id="b202"></a>`OSMO 360 II` | <a id="c202"></a>`OSMO360（供电）开机` |  |  |
| <a id="row-203"></a>203 |  | <a id="b203"></a>`在指定时间 T0，开机` |  |  |  |
| <a id="row-204"></a>204 |  | <a id="b204"></a>`拍摄模式：Timelapse` | <a id="c204"></a>`进入 全景 timelapse，静止延时` | <a id="d204"></a>`adb shell dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x8e 01013f000101 `<br>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c e1 3b` |  |
| <a id="row-205"></a>205 |  | <a id="b205"></a>`输出分辨率：8K` | <a id="c205"></a>`设置分辨率8K` | <a id="d205"></a>`adb shell  dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 6f03000000` |  |
| <a id="row-206"></a>206 |  | <a id="b206"></a>`输出帧率：30 fps` | <a id="c206"></a>`设置帧率30 fps` |  |  |
| <a id="row-207"></a>207 |  | <a id="b207"></a>`拍摄间隔：8s` | <a id="c207"></a>`拍摄间隔8s` | <a id="d207"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 04000050000807000000000000000000` |  |
| <a id="row-208"></a>208 |  | <a id="b208"></a>`持续时间：30分钟` | <a id="c208"></a>`拍摄总时长30分钟` |  |  |
| <a id="row-209"></a>209 |  | <a id="b209"></a>`设置曝光auto` | <a id="c209"></a>`设置曝光auto（iso range 默认）` | <a id="d209"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000100`<br>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1e 0000` |  |
| <a id="row-210"></a>210 |  | <a id="b210"></a>`开始延时摄影` | <a id="c210"></a>`开始延时摄影` | <a id="d210"></a>`adb shell dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01` |  |
