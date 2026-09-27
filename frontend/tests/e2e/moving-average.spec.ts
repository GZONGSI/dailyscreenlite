import { test, expect } from "@playwright/test";
import { movingAverage } from "../../src/features/quotes/series";

test("MA完整窗口：10/20/60非恒定价格，不足周期不补值", () => {
  const bars = Array.from({ length: 61 }, (_, i) => ({
    date: `day-${i}`,
    close: i + 1,
  }));
  expect(movingAverage(bars.slice(0, 9), 10)).toEqual([]);
  const ten = movingAverage(bars, 10);
  expect(ten[0]).toEqual({ date: "day-9", value: 5.5 });
  expect(ten.at(-1)).toEqual({ date: "day-60", value: 56.5 });
  expect(movingAverage(bars, 20)[0].value).toBe(10.5);
  expect(movingAverage(bars, 60)[0].value).toBe(30.5);
  expect(movingAverage(bars, 60)[1].value).toBe(31.5);
});
