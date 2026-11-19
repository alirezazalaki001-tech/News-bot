#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات تلگرام آلارم اخبار فارکس و طلا (برای اجرا روی GitHub Actions، هر ۵ دقیقه یک بار)

- رویدادهای مهم تقویم اقتصادی: چند دقیقه قبل از خبر پیام می‌دهد (لینک + اثر روی طلا)
- خبرهای فوری RSS: بعد از انتشار پیام می‌دهد (فقط خبرهای دارای کلمه کلیدی مهم)
- وضعیت (چه چیزی قبلاً ارسال شده) در state.json ذخیره می‌شود.

متغیرهای محیطی لازم: TELEGRAM_TOKEN و TELEGRAM_CHAT_ID
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ============================ تنظیمات ============================
WINDOW_MINUTES = 12      # رویدادی که تا این‌قدر دقیقه دیگر شروع می‌شود و هنوز اعلام نشده، اعلام می‌شود
IMPACTS = {"High"}       # برای خبرهای متوسط: {"High", "Medium"}
CURRENCIES = {"USD", "AUD", "CHF", "GBP", "NZD", "EUR", "CAD", "JPY"}  # None = همه ارزها
