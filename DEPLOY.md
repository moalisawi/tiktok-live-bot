# نشر البوت على Oracle Cloud Always Free (24/7)

## 1) السيرفر
1. سجّل على https://www.oracle.com/cloud/free/ (لازم بطاقة للتحقق فقط، ما بتنسحب منها فلوس على الـ Always Free).
2. Create instance -> Image: **Ubuntu 22.04** -> Shape: **VM.Standard.E2.1.Micro** (أو Ampere A1) وخليه من الـ Always Free eligible.
3. نزّل مفتاح SSH واتصل: `ssh -i key.pem ubuntu@<IP>`

## 2) رفع البوت
من جهازك (PowerShell):
```
scp -i key.pem -r "D:\كلود\tiktok-live-bot" ubuntu@<IP>:/tmp/bot
```

## 3) التثبيت (على السيرفر) - طريقة systemd
```
sudo apt update && sudo apt install -y python3-venv
sudo mv /tmp/bot /opt/tiktok-live-bot && sudo chown -R ubuntu /opt/tiktok-live-bot
cd /opt/tiktok-live-bot
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env        # حط التوكن
sudo cp tiktok-live-bot.service /etc/systemd/system/
sudo systemctl enable --now tiktok-live-bot
journalctl -u tiktok-live-bot -f          # متابعة السجل
```
بعدها ابعت `/start` للبوت مرة وحدة.

### أو بـ Docker
```
sudo apt install -y docker.io docker-compose-v2
cd /opt/tiktok-live-bot && touch state.json && echo '{}' > state.json
sudo docker compose up -d --build
```

## تحذير مهم
TikTok أحياناً بيحظر عناوين السيرفرات (Datacenter IPs). إذا السجل طلع أخطاء متكررة
مثل "IP blocked" جرّب: region ثاني بأوراكل، أو شغّل البوت على جهازك/راسبيري باي بدل السيرفر.
