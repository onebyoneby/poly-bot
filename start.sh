#!/bin/bash
# Polymarket 统一启动脚本
# 用法:
#   ./start.sh          # DRY_RUN 模式启动所有
#   ./start.sh --live   # 实盘模式
#   ./start.sh stop     # 停止所有

cd "$(dirname "$0")"
PYTHON="/opt/homebrew/bin/python3.10"
PIDFILE="logs/runner.pid"
WEBUI_PIDFILE="logs/webui.pid"
WECHAT_PIDFILE="logs/wechat.pid"

mkdir -p logs

case "${1:-start}" in
  stop)
    echo "正在停止所有服务..."
    if [ -f "$PIDFILE" ]; then
      kill "$(cat $PIDFILE)" 2>/dev/null && echo "引擎已停止" || echo "引擎未运行"
      rm -f "$PIDFILE"
    fi
    if [ -f "$WEBUI_PIDFILE" ]; then
      kill "$(cat $WEBUI_PIDFILE)" 2>/dev/null && echo "WebUI已停止" || echo "WebUI未运行"
      rm -f "$WEBUI_PIDFILE"
    fi
    if [ -f "$WECHAT_PIDFILE" ]; then
      kill "$(cat $WECHAT_PIDFILE)" 2>/dev/null && echo "微信Bot已停止" || echo "微信Bot未运行"
      rm -f "$WECHAT_PIDFILE"
    fi
    # 清理残留
    pkill -f "run_all.py" 2>/dev/null
    pkill -f "streamlit run webui/app.py" 2>/dev/null
    pkill -f "wechat_bot.py" 2>/dev/null
    echo "完成"
    ;;

  status)
    echo "=== Polymarket Bot 状态 ==="
    if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
      echo "引擎: 🟢 运行中 (PID $(cat $PIDFILE))"
    else
      echo "引擎: 🔴 未运行"
    fi
    if [ -f "$WEBUI_PIDFILE" ] && kill -0 "$(cat $WEBUI_PIDFILE)" 2>/dev/null; then
      echo "WebUI: 🟢 运行中 (PID $(cat $WEBUI_PIDFILE)) → http://localhost:8502"
    else
      echo "WebUI: 🔴 未运行"
    fi
    if [ -f "$WECHAT_PIDFILE" ] && kill -0 "$(cat $WECHAT_PIDFILE)" 2>/dev/null; then
      echo "微信Bot: 🟢 运行中 (PID $(cat $WECHAT_PIDFILE))"
    else
      echo "微信Bot: 🔴 未运行 (用 ./start.sh wechat 启动)"
    fi
    ;;

  start|--live)
    # 先停旧进程
    if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
      echo "停止旧引擎..."
      kill "$(cat $PIDFILE)" 2>/dev/null
      sleep 2
    fi

    EXTRA_ARGS=""
    if [ "$1" = "--live" ]; then
      EXTRA_ARGS="--live"
      echo "⚠️  实盘模式!"
    fi

    # 启动统一引擎
    echo "启动统一引擎..."
    nohup $PYTHON run_all.py $EXTRA_ARGS > logs/runner_stdout.log 2>&1 &
    echo $! > "$PIDFILE"
    echo "引擎已启动 (PID $!)"

    # 启动 WebUI
    if [ -f "$WEBUI_PIDFILE" ] && kill -0 "$(cat $WEBUI_PIDFILE)" 2>/dev/null; then
      echo "WebUI 已在运行"
    else
      echo "启动 WebUI..."
      nohup $PYTHON -m streamlit run webui/app.py --server.port 8502 --server.headless true > logs/webui_stdout.log 2>&1 &
      echo $! > "$WEBUI_PIDFILE"
      echo "WebUI 已启动 (PID $!) → http://localhost:8502"
    fi

    echo ""
    echo "✅ 所有服务已启动"
    echo "   查看状态: ./start.sh status"
    echo "   查看日志: tail -f logs/runner_stdout.log"
    echo "   停止服务: ./start.sh stop"
    ;;

  wechat)
    # 微信Bot需要扫码，必须前台运行
    echo "启动微信交互机器人..."
    echo "请用手机微信扫描终端二维码"
    echo "登录后在「文件传输助手」发命令: 报告/持仓/图表/周报/状态/帮助"
    echo ""
    $PYTHON wechat_bot.py ${2:+"$2" "$3"}
    ;;

  wechat-bg)
    # 微信Bot后台运行(已登录过，有缓存时)
    if [ -f "$WECHAT_PIDFILE" ] && kill -0 "$(cat $WECHAT_PIDFILE)" 2>/dev/null; then
      echo "微信Bot 已在运行"
    else
      echo "后台启动微信Bot (需之前已扫码登录过)..."
      nohup $PYTHON wechat_bot.py > logs/wechat_stdout.log 2>&1 &
      echo $! > "$WECHAT_PIDFILE"
      echo "微信Bot 已启动 (PID $!)"
    fi
    ;;

  *)
    echo "用法: $0 [start|stop|status|--live|wechat|wechat-bg]"
    ;;
esac
