import { useMemo, useState } from 'react';

function firstCharacter(value) {
  const text = String(value || '').trim();
  return Array.from(text)[0]?.toLocaleUpperCase() || '?';
}

/**
 * Unified WeChat-style avatar.
 *
 * Avatar URLs are allowed to change after an account refresh.  A failed URL is
 * remembered only for that exact value, so a newly resolved URL is tried
 * immediately while broken images reliably fall back to a local placeholder.
 */
export default function Avatar({
  src,
  name,
  isGroup = false,
  className = 'h-10 w-10',
  imageClassName = '',
  title,
}) {
  const normalizedSrc = typeof src === 'string' ? src.trim() : '';
  const [failedSrc, setFailedSrc] = useState('');
  const showImage = Boolean(normalizedSrc) && failedSrc !== normalizedSrc;
  const accessibleName = String(name || (isGroup ? '群聊' : '未知用户')).trim();
  const fallback = useMemo(() => firstCharacter(accessibleName), [accessibleName]);

  return (
    <span
      className={`inline-flex flex-shrink-0 items-center justify-center overflow-hidden rounded-md bg-wechat-green text-xs font-semibold text-white shadow-sm ${className}`}
      role="img"
      aria-label={`${accessibleName || '未知用户'}的头像`}
      title={title || accessibleName || undefined}
    >
      {showImage ? (
        <img
          src={normalizedSrc}
          alt=""
          aria-hidden="true"
          loading="lazy"
          decoding="async"
          draggable="false"
          referrerPolicy="no-referrer"
          className={`h-full w-full object-cover ${imageClassName}`}
          onError={() => setFailedSrc(normalizedSrc)}
        />
      ) : isGroup ? (
        <svg
          viewBox="0 0 24 24"
          width="62%"
          height="62%"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="9" cy="8" r="3" />
          <circle cx="16.5" cy="9" r="2.5" />
          <path d="M3.5 18c.6-3.2 2.5-5 5.5-5s4.9 1.8 5.5 5" />
          <path d="M14 14c.8-.6 1.7-.9 2.8-.9 2.2 0 3.7 1.4 4.2 3.9" />
        </svg>
      ) : (
        <span aria-hidden="true">{fallback}</span>
      )}
    </span>
  );
}
