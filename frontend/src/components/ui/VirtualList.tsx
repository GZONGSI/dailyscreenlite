import {
  Fragment,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ElementType,
  type RefObject,
} from "react";

interface Props<T> {
  items: T[];
  /** 每行固定高度（像素）：窗口化据此计算可见范围与占位内边距。 */
  rowHeight: number;
  /** 可见范围上下各多渲染几行，滚动时不会露出空白。 */
  overscan?: number;
  /** 实际滚动的祖先元素；缺省时列表自身滚动。 */
  scrollRef?: RefObject<HTMLElement | null>;
  /** 列表容器元素（默认 div），用于保留 ul/ol 等语义结构。 */
  as?: ElementType;
  className?: string;
  /** 列表容器上的语义属性（role / aria-label / data-*）。 */
  containerProps?: Record<string, unknown>;
  /** 每行的内容；index 是该行在完整列表中的位置（0 起）。 */
  children: (item: T, index: number) => React.ReactNode;
}

/**
 * 固定行高的轻量窗口化列表：左侧保留完整列表与任意点选体验，
 * 但只为可见范围渲染真实行，长队列滚动时不同时渲染全部行。
 *
 * 不引入第三方虚拟滚动依赖：行高固定即可用上下内边距精确对齐滚动位置，
 * 行本身不被额外容器包裹，列表语义（role=listbox/option）保持扁平。
 */
export function VirtualList<T>({
  items,
  rowHeight,
  overscan = 2,
  scrollRef,
  as: Container = "div",
  className,
  containerProps,
  children,
}: Props<T>) {
  const own = useRef<HTMLElement | null>(null);
  // 首帧不渲染任何行：渲染范围由挂载后的测量确定。初始渲染整份列表会让满量队列
  // 在首次打开时把所有行都挂载一次，与「只渲染可见行」相反。
  const [range, setRange] = useState({ start: 0, end: 0 });

  const measure = useCallback(() => {
    const node = own.current;
    if (!node) return;
    const scroller = scrollRef?.current ?? node;
    const viewport = scroller.clientHeight || 640;
    // 外部滚动容器下，矩形坐标差已包含滚动位移，不能再加一次 scrollTop。
    const top = Math.max(0, scroller === node
      ? scroller.scrollTop
      : scroller.getBoundingClientRect().top + scroller.clientTop - node.getBoundingClientRect().top);
    const visible = Math.ceil(viewport / rowHeight) + 1;
    const start = Math.max(0, Math.floor(top / rowHeight) - overscan);
    setRange({ start, end: Math.min(items.length, start + visible + overscan * 2) });
  }, [items.length, overscan, rowHeight, scrollRef]);

  useEffect(() => {
    measure();
  }, [measure]);

  useEffect(() => {
    const scroller = scrollRef?.current ?? own.current;
    if (!scroller) return;
    const observer =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => measure());
    observer?.observe(scroller);
    scroller.addEventListener("scroll", measure, { passive: true });
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      scroller.removeEventListener("scroll", measure);
      window.removeEventListener("resize", measure);
    };
  }, [measure, scrollRef]);

  const total = items.length;
  const start = Math.min(range.start, Math.max(0, total - 1));
  const end = Math.max(start, Math.min(range.end, total));
  const visible = items.slice(start, end);

  return (
    <Container
      ref={own}
      onScroll={scrollRef ? undefined : measure}
      className={className}
      data-virtual-start={start}
      data-virtual-total={total}
      style={{
        paddingTop: start * rowHeight,
        paddingBottom: Math.max(0, (total - end) * rowHeight),
      }}
      {...containerProps}
    >
      {visible.map((item, offset) => (
        <Fragment key={start + offset}>{children(item, start + offset)}</Fragment>
      ))}
    </Container>
  );
}
