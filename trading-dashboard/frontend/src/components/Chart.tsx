import React, { useEffect, useRef, useState } from 'react';
import {
  createChart,
  ColorType,
  IChartApi,
  ISeriesApi,
  LineStyle,
} from 'lightweight-charts';
import { Bar, IndicatorValues } from '../types';

interface ChartProps {
  bars: Bar[];
  indicators: IndicatorValues | null;
  ticker: string;
  mainHeight?: number;
  panelHeight?: number;
  showVolume?: boolean;
  showMA?: boolean;
}

export const Chart: React.FC<ChartProps> = ({
  bars,
  indicators,
  ticker,
  mainHeight = 500,
  panelHeight = 120,
  showVolume = true,
  showMA = true,
}) => {
  const chartContainer = useRef<HTMLDivElement | null>(null);
  const volumeContainer = useRef<HTMLDivElement | null>(null);
  const rsiContainer = useRef<HTMLDivElement | null>(null);
  const macdContainer = useRef<HTMLDivElement | null>(null);

  const mainChart = useRef<IChartApi | null>(null);
  const volumeChart = useRef<IChartApi | null>(null);
  const rsiChart = useRef<IChartApi | null>(null);
  const macdChart = useRef<IChartApi | null>(null);

  const candleSeries = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeSeries = useRef<ISeriesApi<'Histogram'> | null>(null);

  const sma20Series = useRef<ISeriesApi<'Line'> | null>(null);
  const sma50Series = useRef<ISeriesApi<'Line'> | null>(null);
  const sma200Series = useRef<ISeriesApi<'Line'> | null>(null);
  const ema12Series = useRef<ISeriesApi<'Line'> | null>(null);
  const ema26Series = useRef<ISeriesApi<'Line'> | null>(null);

  const rsiSeries = useRef<ISeriesApi<'Line'> | null>(null);
  const macdLineSeries = useRef<ISeriesApi<'Line'> | null>(null);
  const macdSignalSeries = useRef<ISeriesApi<'Line'> | null>(null);
  const macdHistSeries = useRef<ISeriesApi<'Histogram'> | null>(null);

  // Rolling indicator history keyed by ticker, so a ticker switch starts
  // fresh and series don't carry over.
  const indicatorHistory = useRef<{
    ticker: string;
    sma_20: { time: number; value: number }[];
    sma_50: { time: number; value: number }[];
    sma_200: { time: number; value: number }[];
    ema_12: { time: number; value: number }[];
    ema_26: { time: number; value: number }[];
    rsi_14: { time: number; value: number }[];
    macd_line: { time: number; value: number }[];
    macd_signal: { time: number; value: number }[];
    macd_hist: { time: number; value: number; color: string }[];
  }>({
    ticker: '',
    sma_20: [], sma_50: [], sma_200: [],
    ema_12: [], ema_26: [],
    rsi_14: [],
    macd_line: [], macd_signal: [], macd_hist: [],
  });

  // Initialize main chart
  useEffect(() => {
    if (!chartContainer.current) return;

    const chart_inst = createChart(chartContainer.current, {
      layout: {
        textColor: '#d1d5db',
        background: { type: ColorType.Solid, color: '#1f2937' },
      },
      width: chartContainer.current.clientWidth,
      height: mainHeight,
      timeScale: { timeVisible: true, secondsVisible: true },
      grid: { vertLines: { color: '#374151' }, horzLines: { color: '#374151' } },
    });
    mainChart.current = chart_inst;

    // Candles
    const candle = chart_inst.addCandlestickSeries({
      upColor: '#10b981',
      downColor: '#ef4444',
      wickUpColor: '#10b981',
      wickDownColor: '#ef4444',
      borderVisible: false,
    });
    candleSeries.current = candle;

    // Moving Averages
    const sma20 = chart_inst.addLineSeries({
      color: '#f59e0b',
      lineWidth: 1,
      title: 'SMA(20)',
    });
    sma20Series.current = sma20;

    const sma50 = chart_inst.addLineSeries({
      color: '#06b6d4',
      lineWidth: 1,
      title: 'SMA(50)',
    });
    sma50Series.current = sma50;

    const sma200 = chart_inst.addLineSeries({
      color: '#8b5cf6',
      lineWidth: 2,
      lineStyle: LineStyle.Dashed,
      title: 'SMA(200)',
    });
    sma200Series.current = sma200;

    const ema12 = chart_inst.addLineSeries({
      color: '#ec4899',
      lineWidth: 1,
      title: 'EMA(12)',
    });
    ema12Series.current = ema12;

    const ema26 = chart_inst.addLineSeries({
      color: '#14b8a6',
      lineWidth: 1,
      title: 'EMA(26)',
    });
    ema26Series.current = ema26;

    chart_inst.timeScale().fitContent();

    const handleResize = () => {
      if (chartContainer.current) {
        chart_inst.applyOptions({ width: chartContainer.current.clientWidth });
      }
    };

    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
      chart_inst.remove();
      mainChart.current = null;
    };
  }, [mainHeight]);

  // Initialize volume chart
  useEffect(() => {
    if (!volumeContainer.current || !showVolume) return;

    const chart_inst = createChart(volumeContainer.current, {
      layout: {
        textColor: '#d1d5db',
        background: { type: ColorType.Solid, color: '#1f2937' },
      },
      width: volumeContainer.current.clientWidth,
      height: panelHeight,
      timeScale: { timeVisible: false },
      grid: { vertLines: { color: '#374151' }, horzLines: { color: '#374151' } },
    });
    volumeChart.current = chart_inst;

    const volume = chart_inst.addHistogramSeries({
      color: '#6366f133',
      title: 'Volume',
    });
    volumeSeries.current = volume;

    chart_inst.timeScale().fitContent();

    const handleResize = () => {
      if (volumeContainer.current) {
        chart_inst.applyOptions({ width: volumeContainer.current.clientWidth });
      }
    };

    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
      chart_inst.remove();
      volumeChart.current = null;
    };
  }, [showVolume, panelHeight]);

  // Initialize RSI chart
  useEffect(() => {
    if (!rsiContainer.current) return;

    const chart_inst = createChart(rsiContainer.current, {
      layout: {
        textColor: '#d1d5db',
        background: { type: ColorType.Solid, color: '#1f2937' },
      },
      width: rsiContainer.current.clientWidth,
      height: panelHeight,
      timeScale: { timeVisible: false },
      grid: { vertLines: { color: '#374151' }, horzLines: { color: '#374151' } },
    });
    rsiChart.current = chart_inst;

    const rsi = chart_inst.addLineSeries({
      color: '#3b82f6',
      lineWidth: 2,
      title: 'RSI(14)',
    });
    rsiSeries.current = rsi;

    // Add overbought/oversold levels
    const overbought = chart_inst.addLineSeries({
      color: '#ef444433',
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
    });
    overbought.setData([]);

    const oversold = chart_inst.addLineSeries({
      color: '#10b98133',
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
    });
    oversold.setData([]);

    chart_inst.priceScale('right').setScaleMargins(0.1, 0.1);
    chart_inst.timeScale().fitContent();

    const handleResize = () => {
      if (rsiContainer.current) {
        chart_inst.applyOptions({ width: rsiContainer.current.clientWidth });
      }
    };

    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
      chart_inst.remove();
      rsiChart.current = null;
    };
  }, [panelHeight]);

  // Initialize MACD chart
  useEffect(() => {
    if (!macdContainer.current) return;

    const chart_inst = createChart(macdContainer.current, {
      layout: {
        textColor: '#d1d5db',
        background: { type: ColorType.Solid, color: '#1f2937' },
      },
      width: macdContainer.current.clientWidth,
      height: panelHeight,
      timeScale: { timeVisible: false },
      grid: { vertLines: { color: '#374151' }, horzLines: { color: '#374151' } },
    });
    macdChart.current = chart_inst;

    const macdLine = chart_inst.addLineSeries({
      color: '#3b82f6',
      lineWidth: 2,
      title: 'MACD Line',
    });
    macdLineSeries.current = macdLine;

    const signal = chart_inst.addLineSeries({
      color: '#f59e0b',
      lineWidth: 2,
      title: 'Signal',
    });
    macdSignalSeries.current = signal;

    const hist = chart_inst.addHistogramSeries({
      color: '#6b7280',
      title: 'Histogram',
    });
    macdHistSeries.current = hist;

    chart_inst.timeScale().fitContent();

    const handleResize = () => {
      if (macdContainer.current) {
        chart_inst.applyOptions({ width: macdContainer.current.clientWidth });
      }
    };

    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
      chart_inst.remove();
      macdChart.current = null;
    };
  }, [panelHeight]);

  // Update all chart data
  useEffect(() => {
    if (!bars.length) return;

    // Bar.timestamp is in Unix epoch seconds; lightweight-charts expects
    // the same format.
    const barTime = (ts: number) => ts as any;

    const candleData = bars.map((bar) => ({
      time: barTime(bar.timestamp),
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));

    const volumeData = bars.map((bar) => ({
      time: barTime(bar.timestamp),
      value: bar.volume,
      color: bar.close >= bar.open ? '#10b98166' : '#ef444466',
    }));

    if (candleSeries.current) candleSeries.current.setData(candleData);
    if (volumeSeries.current) volumeSeries.current.setData(volumeData);

    // If the ticker changed, reset the rolling indicator history so we don't
    // mix series across tickers.
    if (indicatorHistory.current.ticker !== ticker) {
      indicatorHistory.current = {
        ticker,
        sma_20: [], sma_50: [], sma_200: [],
        ema_12: [], ema_26: [],
        rsi_14: [],
        macd_line: [], macd_signal: [], macd_hist: [],
      };
    }

    const hist = indicatorHistory.current;
    const lastTime = barTime(bars[bars.length - 1].timestamp);

    // Helper: append a new (time, value) point; update-in-place if the
    // newest time matches, so each series stays monotonically ordered.
    const push = (
      arr: { time: number; value: number }[],
      value: number
    ) => {
      if (!Number.isFinite(value) || value <= 0) return;
      if (arr.length > 0 && arr[arr.length - 1].time === lastTime) {
        arr[arr.length - 1] = { time: lastTime, value };
      } else {
        arr.push({ time: lastTime, value });
      }
      if (arr.length > 500) arr.splice(0, arr.length - 500);
    };

    if (indicators) {
      if (showMA) {
        push(hist.sma_20, indicators.sma_20);
        push(hist.sma_50, indicators.sma_50);
        push(hist.sma_200, indicators.sma_200);
        push(hist.ema_12, indicators.ema_12);
        push(hist.ema_26, indicators.ema_26);

        if (sma20Series.current)  sma20Series.current.setData(hist.sma_20 as any);
        if (sma50Series.current)  sma50Series.current.setData(hist.sma_50 as any);
        if (sma200Series.current) sma200Series.current.setData(hist.sma_200 as any);
        if (ema12Series.current)  ema12Series.current.setData(hist.ema_12 as any);
        if (ema26Series.current)  ema26Series.current.setData(hist.ema_26 as any);
      }

      push(hist.rsi_14, indicators.rsi_14);
      if (rsiSeries.current) rsiSeries.current.setData(hist.rsi_14 as any);

      // MACD line/signal can be negative — don't filter at zero.
      const pushSigned = (arr: { time: number; value: number }[], value: number) => {
        if (!Number.isFinite(value)) return;
        if (arr.length > 0 && arr[arr.length - 1].time === lastTime) {
          arr[arr.length - 1] = { time: lastTime, value };
        } else {
          arr.push({ time: lastTime, value });
        }
        if (arr.length > 500) arr.splice(0, arr.length - 500);
      };
      pushSigned(hist.macd_line, indicators.macd_line);
      pushSigned(hist.macd_signal, indicators.macd_signal);

      const histColor = indicators.macd_histogram >= 0 ? '#10b98166' : '#ef444466';
      if (Number.isFinite(indicators.macd_histogram)) {
        const entry = { time: lastTime, value: indicators.macd_histogram, color: histColor };
        if (hist.macd_hist.length > 0 && hist.macd_hist[hist.macd_hist.length - 1].time === lastTime) {
          hist.macd_hist[hist.macd_hist.length - 1] = entry;
        } else {
          hist.macd_hist.push(entry);
        }
        if (hist.macd_hist.length > 500) hist.macd_hist.splice(0, hist.macd_hist.length - 500);
      }

      if (macdLineSeries.current)   macdLineSeries.current.setData(hist.macd_line as any);
      if (macdSignalSeries.current) macdSignalSeries.current.setData(hist.macd_signal as any);
      if (macdHistSeries.current)   macdHistSeries.current.setData(hist.macd_hist as any);
    }

    // Fit all charts
    mainChart.current?.timeScale().fitContent();
    volumeChart.current?.timeScale().fitContent();
    rsiChart.current?.timeScale().fitContent();
    macdChart.current?.timeScale().fitContent();
  }, [bars, indicators, showMA, showVolume, ticker]);

  return (
    <div className="space-y-1">
      <div ref={chartContainer} style={{ width: '100%', height: `${mainHeight}px` }} className="bg-gray-900 rounded-lg border border-gray-700" />
      {showVolume && <div ref={volumeContainer} style={{ width: '100%', height: `${panelHeight}px` }} className="bg-gray-900 border border-gray-700" />}
      <div ref={rsiContainer} style={{ width: '100%', height: `${panelHeight}px` }} className="bg-gray-900 border border-gray-700" />
      <div ref={macdContainer} style={{ width: '100%', height: `${panelHeight}px` }} className="bg-gray-900 rounded-b-lg border border-gray-700" />
    </div>
  );
};
