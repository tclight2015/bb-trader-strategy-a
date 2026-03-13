# BB Grid Trader — 策略A

Bollinger Band 網格交易系統，整合 BB Scanner + 自動交易引擎。

## 策略說明

**篩選階段（15分K）**
- 掃描所有USDT永續合約
- 篩選15分K距布林帶上軌在設定範圍內的幣種
- 同時檢查1小時K距上軌條件
- 符合條件的幣種進入候選監控池

**開倉執行（1分K）**
- 候選池幣種切換到1分K監控
- 以1分K上軌為中間線執行開倉邏輯

**往下（順勢）**
- 1分K價格觸碰布林帶上軌開第一單空倉
- 往下設4個固定間距網格（0.15%），穿越後回彈觸碰再開空單
- 每次開倉後以當根1分K棒高點重建下方網格

**往上（逆勢）— 策略A**
- 價格往上走不追，只觀望
- 等1分K出現黑K（收盤<開盤）
- 取黑K本身+前2根 共3根 最高點 = 新空單點位
- 掛限價單等待觸碰

**平倉**
- 幣種累積所有倉位計算平均成本
- 達到止盈本金% → 市價平倉該幣種所有倉位

## 保護機制

- 保證金使用率超過閾值 → 停止開新倉
- 單幣種本金虧損超過暫停閾值 → 停止對該幣種開新倉
- 單幣種本金虧損超過強制平倉閾值 → 市價強制全平
- ROE 回升後自動恢復開倉

## 檔案結構

```
app.py          # Flask 主程式 + API 路由
trader.py       # 交易引擎（策略A邏輯）
binance_client.py  # Binance Testnet API 封裝
database.py     # SQLite 資料庫操作
config.py       # 設定管理
templates/
  index.html    # 儀表板（明亮色系）
```

## 本地執行

```bash
pip install -r requirements.txt
python app.py
```

## 部署到 Railway

1. 建立 GitHub Repository，上傳所有檔案
2. 去 railway.app 用 GitHub 登入
3. New Project → Deploy from GitHub repo
4. 部署完成後取得網址

## 儀表板功能

- **儀表板**：帳戶資訊、保證金水位、持倉狀態、系統控制
- **掃描器**：BB Scanner（現有功能保留）
- **報表**：累積損益曲線、歷史交易、每日績效
- **設定**：所有交易參數可調整

## 注意事項

- 目前接 Binance Testnet，不涉及真實資金
- API Key 請存在 trading_config.json（已初始化）
- 出入金前請使用暫停鍵，等所有倉位平倉後再操作
