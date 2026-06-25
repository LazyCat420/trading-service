import yfinance as yf
print(yf.Ticker("AAPL").info.get("exchange"))
print(yf.Ticker("003160.KS").info.get("exchange"))
