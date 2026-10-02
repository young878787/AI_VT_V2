/** Rushia 半身構圖；縮放時以頭頂安全區為錨點。 */
export function resolveHalfBodyFraming(width: number, height: number, zoom = 1) {
  const aspect = width > 0 && height > 0 ? width / height : 1;
  const scale = Math.min(2.05, Math.max(1.2, aspect * 3.15))
    * Math.max(0.75, Math.min(1.25, zoom));
  return { scale, y: 0.86 - 0.98 * scale };
}
