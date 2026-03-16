"""
Phase 2: Sentiment Analysis Layer.

Uses Alpaca News API to fetch recent headlines for watchlist symbols.
Scores articles using keyword-based analysis and applies a sentiment
weight to the signal engine's buy/sell decisions.

Architecture:
    - get_sentiment(symbol) → float score (-1.0 to +1.0)
    - adjust_signal_with_sentiment(signal_dict) → modified signal dict
    - Caching: 5-minute TTL per symbol to avoid API spam
"""

import logging
import time
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from core import alpaca_client
import config

logger = logging.getLogger(__name__)

# In-memory sentiment cache: {symbol: {"score": float, "articles": int, "timestamp": float}}
_sentiment_cache: Dict[str, Dict[str, Any]] = {}


def _score_headline(headline: str) -> float:
    """
    Score a single headline based on keyword matching.
    Returns a value between -1.0 and +1.0.
    """
    headline_lower = headline.lower()
    pos_hits = 0
    neg_hits = 0

    for kw in config.SENTIMENT_KEYWORDS_POSITIVE:
        if kw.lower() in headline_lower:
            pos_hits += 1
    for kw in config.SENTIMENT_KEYWORDS_NEGATIVE:
        if kw.lower() in headline_lower:
            neg_hits += 1

    total = pos_hits + neg_hits
    if total == 0:
        return 0.0

    # Net score normalized to [-1, 1]
    raw = (pos_hits - neg_hits) / total
    return max(-1.0, min(1.0, raw))


def _fetch_news(symbol: str) -> List[Dict[str, Any]]:
    """
    Fetch recent news articles for a symbol from Alpaca News API.
    Returns a list of {"headline": str, "timestamp": str, "source": str, "url": str}.
    """
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import NewsRequest

        client = StockHistoricalDataClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY
        )
        # Alpaca News API
        from alpaca.data.historical.news import NewsClient
        news_client = NewsClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)

        request = NewsRequest(
            symbols=symbol,
            limit=config.ALPACA_NEWS_LIMIT,
        )
        news = news_client.get_news(request)

        articles = []
        if news and hasattr(news, 'news'):
            for article in news.news:
                articles.append({
                    "headline": article.headline,
                    "timestamp": article.created_at.isoformat() if article.created_at else None,
                    "source": article.source if hasattr(article, 'source') else "unknown",
                    "url": article.url if hasattr(article, 'url') else None,
                })
        return articles

    except ImportError:
        # Fallback: use trading client's REST API directly
        return _fetch_news_rest(symbol)
    except Exception as e:
        logger.debug(f"Alpaca News SDK failed for {symbol}, trying REST fallback: {e}")
        return _fetch_news_rest(symbol)


def _fetch_news_rest(symbol: str) -> List[Dict[str, Any]]:
    """Fallback: fetch news via Alpaca REST API using requests/httpx."""
    try:
        import httpx

        url = "https://data.alpaca.markets/v1beta1/news"
        headers = {
            "APCA-API-KEY-ID": config.ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
        }
        params = {
            "symbols": symbol,
            "limit": config.ALPACA_NEWS_LIMIT,
            "sort": "desc",
        }

        resp = httpx.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        articles = []
        for item in data.get("news", []):
            articles.append({
                "headline": item.get("headline", ""),
                "timestamp": item.get("created_at"),
                "source": item.get("source", "unknown"),
                "url": item.get("url"),
            })
        return articles

    except Exception as e:
        logger.warning(f"Failed to fetch news for {symbol}: {e}")
        return []


def get_sentiment(symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
    """
    Get sentiment score for a symbol.

    Returns:
        {
            "symbol": str,
            "score": float (-1.0 to +1.0),
            "label": "bullish" | "bearish" | "neutral",
            "articles_analyzed": int,
            "headlines": list of scored headlines,
            "cached": bool,
        }
    """
    if not config.ENABLE_SENTIMENT:
        return {
            "symbol": symbol, "score": 0.0, "label": "neutral",
            "articles_analyzed": 0, "headlines": [], "cached": False,
        }

    # Check cache
    now = time.time()
    cached = _sentiment_cache.get(symbol)
    if cached and not force_refresh:
        age = now - cached["timestamp"]
        if age < config.SENTIMENT_CACHE_TTL_SECONDS:
            return {**cached["data"], "cached": True}

    # Fetch and score
    articles = _fetch_news(symbol)
    scored_headlines = []
    total_score = 0.0

    for article in articles:
        headline = article.get("headline", "")
        if not headline:
            continue
        score = _score_headline(headline)
        scored_headlines.append({
            "headline": headline,
            "score": round(score, 3),
            "source": article.get("source", ""),
            "timestamp": article.get("timestamp"),
        })
        total_score += score

    count = len(scored_headlines)
    avg_score = (total_score / count) if count > 0 else 0.0

    # Label
    if avg_score >= config.SENTIMENT_BULLISH_THRESHOLD:
        label = "bullish"
    elif avg_score <= config.SENTIMENT_BEARISH_THRESHOLD:
        label = "bearish"
    else:
        label = "neutral"

    result = {
        "symbol": symbol,
        "score": round(avg_score, 4),
        "label": label,
        "articles_analyzed": count,
        "headlines": scored_headlines[:5],  # Top 5 for display
        "cached": False,
    }

    # Update cache
    _sentiment_cache[symbol] = {
        "data": result,
        "timestamp": now,
    }

    return result


def get_batch_sentiment(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Get sentiment for a list of symbols. Returns dict keyed by symbol."""
    results = {}
    for symbol in symbols:
        results[symbol] = get_sentiment(symbol)
    return results


def adjust_signal_with_sentiment(signal: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adjust a signal dict based on sentiment analysis.

    Rules:
        - If signal is 'buy' and sentiment is bearish → downgrade to 'hold'
        - If signal is 'hold' and sentiment is strongly bullish → keep as hold but note it
        - Adds 'sentiment_score', 'sentiment_label' fields to signal dict

    The weight is controlled by config.SENTIMENT_WEIGHT.
    """
    symbol = signal.get("symbol")
    if not symbol or not config.ENABLE_SENTIMENT:
        return signal

    sentiment = get_sentiment(symbol)
    signal["sentiment_score"] = sentiment["score"]
    signal["sentiment_label"] = sentiment["label"]
    signal["sentiment_articles"] = sentiment["articles_analyzed"]

    current_signal = signal.get("signal", "hold")

    # Block buy signals when sentiment is strongly bearish
    if current_signal == "buy" and sentiment["label"] == "bearish":
        signal["signal"] = "hold"
        signal["reason"] = (
            f"{signal.get('reason', '')} | BLOCKED by bearish sentiment "
            f"(score: {sentiment['score']:.3f}, {sentiment['articles_analyzed']} articles)"
        )
        logger.info(f"[{symbol}] Buy signal blocked by bearish sentiment: {sentiment['score']:.3f}")

    # Boost confidence note for bullish sentiment on buy signals
    elif current_signal == "buy" and sentiment["label"] == "bullish":
        signal["reason"] = (
            f"{signal.get('reason', '')} | Confirmed by bullish sentiment "
            f"(score: {sentiment['score']:.3f})"
        )

    return signal


def clear_cache():
    """Clear the sentiment cache."""
    global _sentiment_cache
    _sentiment_cache.clear()
