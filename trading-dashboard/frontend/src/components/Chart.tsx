import React, { useEffect, useRef } from 'react';
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
  height?: number;
}

export const Chart: React.FC<ChartProps> = ({ bars, indicators, ticker, height = 600 }) => {
  const chartContainer = useRef<HTMLDivElement | null>(null);
  const chart = useRef<IChartApi | null>(null);
  const candleSeries = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const bbUpperSeries = useRef<ISeriesApi<'Line'> | null>(null);
  const bbLowerSeries = useRef<ISeriesApi<'Line'> | null>(null);
  const bbMiddleSeries = useRef<ISeriesApi<'Line'> | null>(null);

  // Initialize chart
  useEffect(() => {
    if (!chartContainer.current) return;

    const chart_inst = createChart(chartContainer.current, {
      layout: {
        textColor: '#d1d5db',
        background: { type: ColorType.Solid, color: '#1f2937' },
      },
      width: chartContainer.current.clientWidth,
      height: height,
      timeScale: {
        timeVisible: true,
        secondsVisible: true,
      },
      grid: {
        vertLines: { color: '#374151' },
        horzLines: { color: '#374151' },
      },
    });

    chart.current = chart_inst;

    // Candle series
    const candle = chart_inst.addCandlestickSeries({
      upColor: '#10b981',
      downColor: '#ef4444',
      wickUpColor: '#10b981',
      wickDownColor: '#ef4444',
      borderVisible: false,
    });
    candleSeries.current = candle;

    // Bollinger Bands
    const bbUpper = chart_inst.addLineSeries({
      color: '#8b5cf6',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      title: 'BB Upper',
    });
    bbUpperSeries.current = bbUpper;

    const bbLower = chart_inst.addLineSeries({
      color: '#8b5cf6',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      title: 'BB Lower',
    });
    bbLowerSeries.current = bbLower;

    const bbMiddle = chart_inst.addLineSeries({
      color: '#6366f1',
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      title: 'BB Middle',
    });
    bbMiddleSeries.current = bbMiddle;

    // Fit content
    chart_inst.timeScale().fitContent();

    // Handle resize
    const handleResize = () => {
      if (chartContainer.current) {
        chart_inst.applyOptions({
          width: chartContainer.current.clientWidth,
        });
      }
    };

    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      chart_inst.remove();
      chart.current = null;
    };
  }, [height]);

  // Update chart data
  useEffect(() => {
    if (!candleSeries.current || !bbUpperSeries.current || !bars.length) return;

    // Convert bars to chart format (time in seconds)
    const candleData = bars.map((bar) => ({
      time: Math.floor(bar.timestamp / 1000) as any,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));

    candleSeries.current.setData(candleData);

    // Add Bollinger Bands if we have indicators for the latest bar
    if (indicators) {
      const bbData = bars.map((bar) => {
        // For simplicity, we'll show BB for all bars but only have accurate values for latest
        // In production, compute BB for each bar
        return {
          time: Math.floor(bar.timestamp / 1000) as any,
          value: indicators.bb_upper_20, // placeholder
        };
      });

      // Only show BB for the last bar to avoid confusion
      const lastBar = bars[bars.length - 1];
      if (lastBar) {
        const lastTime = Math.floor(lastBar.timestamp / 1000) as any;

        bbUpperSeries.current.setData([
          { time: lastTime, value: indicators.bb_upper_20 },
        ]);
        bbLowerSeries.current.setData([
          { time: lastTime, value: indicators.bb_lower_20 },
        ]);
        bbMiddleSeries.current.setData([
          { time: lastTime, value: indicators.bb_middle_20 },
        ]);
      }
    }

    // Auto-scroll to latest
    if (chart.current) {
      chart.current.timeScale().fitContent();
    }
  }, [bars, indicators]);

  return (
    <div
      ref={chartContainer}
      style={{ width: '100%', height: `${height}px` }}
      className="bg-gray-900 rounded-lg border border-gray-700"
    />
  );
};
