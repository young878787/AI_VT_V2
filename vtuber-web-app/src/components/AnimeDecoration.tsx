/**
 * 背景裝飾 — 淡藍色風格光暈
 */
import './AnimeDecoration.css';

export const AnimeDecoration = () => {
  return (
    <div className="bg-decoration" aria-hidden="true">
      <div className="bg-glow bg-glow--tr" />
      <div className="bg-glow bg-glow--bl" />
    </div>
  );
};
