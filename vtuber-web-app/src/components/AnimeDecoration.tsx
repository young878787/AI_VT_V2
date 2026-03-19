/**
 * 日式動漫風格裝飾元件
 * 添加浮動的櫻花、星星等視覺效果
 */
import './AnimeDecoration.css';

export const AnimeDecoration = () => {
  return (
    <div className="anime-decoration">
      {/* 浮動櫻花 */}
      <div className="sakura-container">
        {[...Array(8)].map((_, i) => (
          <div 
            key={`sakura-${i}`} 
            className="sakura"
            style={{
              left: `${Math.random() * 100}%`,
              animationDelay: `${Math.random() * 10}s`,
              animationDuration: `${15 + Math.random() * 10}s`,
            }}
          >
            🌸
          </div>
        ))}
      </div>

      {/* 閃爍星星 */}
      <div className="star-container">
        {[...Array(12)].map((_, i) => (
          <div 
            key={`star-${i}`} 
            className="star"
            style={{
              left: `${Math.random() * 100}%`,
              top: `${Math.random() * 100}%`,
              animationDelay: `${Math.random() * 3}s`,
            }}
          >
            ✨
          </div>
        ))}
      </div>

      {/* 漂浮泡泡 */}
      <div className="bubble-container">
        {[...Array(6)].map((_, i) => (
          <div 
            key={`bubble-${i}`} 
            className="bubble"
            style={{
              left: `${10 + Math.random() * 80}%`,
              animationDelay: `${Math.random() * 8}s`,
              animationDuration: `${10 + Math.random() * 5}s`,
            }}
          />
        ))}
      </div>
    </div>
  );
};
