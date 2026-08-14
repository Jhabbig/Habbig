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

    const candleData = bars.map((bar) => ({
      time: Math.floor(bar.timestamp / 1000) as any,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));

    const volumeData = bars.map((bar) => ({
      time: Math.floor(bar.timestamp / 1000) as any,
      value: bar.volume,
      color: bar.close >= bar.open ? '#10b98166' : '#ef444466',
    }));

    if (candleSeries.current) candleSeries.current.setData(candleData);
    if (volumeSeries.current) volumeSeries.current.setData(volumeData);

    // Update moving averages
    if (indicators && showMA) {
      const smaData = indicators.sma_20 > 0 ? [{ time: Math.floor(bars[bars.length - 1].timestamp / 1000) as any, value: indicators.sma_20 }] : [];
      const ema12Data = indicators.ema_12 > 0 ? [{ time: Math.floor(bars[bars.length - 1].timestamp / 1000) as any, value: indicators.ema_12 }] : [];

      if (sma20Series.current && indicators.sma_20 > 0) sma20Series.current.setData([{ time: Math.floor(bars[bars.length - 1].timestamp / 1000) as any, value: indicators.sma_20 }]);
      if (sma50Series.current && indicators.sma_50 > 0) sma50Series.current.setData([{ time: Math.floor(bars[bars.length - 1].timestamp / 1000) as any, value: indicators.sma_50 }]);
      if (sma200Series.current && indicators.sma_200 > 0) sma200Series.current.setData([{ time: Math.floor(bars[bars.length - 1].timestamp / 1000) as any, value: indicators.sma_200 }]);
      if (ema12Series.current && indicators.ema_12 > 0) ema12Series.current.setData([{ time: Math.floor(bars[bars.length - 1].timestamp / 1000) as any, value: indicators.ema_12 }]);
      if (ema26Series.current && indicators.ema_26 > 0) ema26Series.current.setData([{ time: Math.floor(bars[bars.length - 1].timestamp / 1000) as any, value: indicators.ema_26 }]);
    }

    // Update RSI
    if (indicators && rsiSeries.current) {
      rsiSeries.current.setData([{ time: Math.floor(bars[bars.length - 1].timestamp / 1000) as any, value: indicators.rsi_14 }]);
    }

    // Update MACD
    if (indicators) {
      const lastTime = Math.floor(bars[bars.length - 1].timestamp / 1000) as any;
      if (macdLineSeries.current) macdLineSeries.current.setData([{ time: lastTime, value: indicators.macd_line }]);
      if (macdSignalSeries.current) macdSignalSeries.current.setData([{ time: lastTime, value: indicators.macd_signal }]);
      if (macdHistSeries.current) {
        const histColor = indicators.macd_histogram > 0 ? '#10b98166' : '#ef444466';
        macdHistSeries.current.setData([{ time: lastTime, value: indicators.macd_histogram, color: histColor }]);
      }
    }

    // Fit all charts
    mainChart.current?.timeScale().fitContent();
    volumeChart.current?.timeScale().fitContent();
    rsiChart.current?.timeScale().fitContent();
    macdChart.current?.timeScale().fitContent();
  }, [bars, indicators, showMA, showVolume]);

  return (
    <div className="space-y-1">
      <div ref={chartContainer} style={{ width: '100%', height: `${mainHeight}px` }} className="bg-gray-900 rounded-lg border border-gray-700" />
      {showVolume && <div ref={volumeContainer} style={{ width: '100%', height: `${panelHeight}px` }} className="bg-gray-900 border border-gray-700" />}
      <div ref={rsiContainer} style={{ width: '100%', height: `${panelHeight}px` }} className="bg-gray-900 border border-gray-700" />
      <div ref={macdContainer} style={{ width: '100%', height: `${panelHeight}px` }} className="bg-gray-900 rounded-b-lg border border-gray-700" />
    </div>
  );
};
