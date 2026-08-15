# MQTT Broker (Mosquitto) 搭建指引

## Windows

```powershell
winget install --id EclipseFoundation.Mosquitto --accept-source-agreements --accept-package-agreements
```

安装后编辑 `C:\Program Files\mosquitto\mosquitto.conf`，追加（局域网原型阶段允许匿名）：

```conf
listener 1883 0.0.0.0
allow_anonymous true
```

以管理员身份重启服务：

```powershell
net stop mosquitto; net start mosquitto
```

## Linux (Debian/Ubuntu)

```bash
sudo apt install mosquitto mosquitto-clients
sudo tee /etc/mosquitto/conf.d/lan.conf <<'EOF'
listener 1883 0.0.0.0
allow_anonymous true
EOF
sudo systemctl restart mosquitto
```

## Docker（任意平台）

```bash
docker run -d --name mosquitto --restart always -p 1883:1883 \
  eclipse-mosquitto:2 sh -c "echo -e 'listener 1883 0.0.0.0\nallow_anonymous true' > /mosquitto/config/mosquitto.conf && /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf"
```

## 验证

```bash
mosquitto_sub -h 127.0.0.1 -t "test/#" -v   # 终端1
mosquitto_pub -h 127.0.0.1 -t "test/hello" -m "hi"   # 终端2
```

> 局域网内其他机器访问时，把 `127.0.0.1` 换成 Broker 机器的局域网 IP，并在防火墙放行 1883 端口。
