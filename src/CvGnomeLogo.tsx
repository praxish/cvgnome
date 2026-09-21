// SPDX-License-Identifier: MPL-2.0
type LogoProps = {
  compact?: boolean;
};

export function CvGnomeMark({ decorative = false }: { decorative?: boolean }) {
  return (
    <svg
      className="cvgnome-mark"
      viewBox="0 0 64 72"
      role={decorative ? undefined : "img"}
      aria-hidden={decorative || undefined}
      aria-label={decorative ? undefined : "CVGnome trail marker"}
      focusable="false"
    >
      <path
        d="M32 4C20 11 11 27 8 44c5 8 13 17 24 25 11-8 19-17 24-25C53 27 44 11 32 4Z"
        fill="#356A8D"
      />
      <path
        d="M9.5 44.8c8.7-5.7 15.3-4 21.9 5.8 6.7-9.7 13.6-16.5 23.1-21.4"
        fill="none"
        stroke="#FFFDF8"
        strokeWidth="7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="32" cy="27" r="7" fill="#1C1A17" />
      <circle cx="32" cy="27" r="4.8" fill="#D96F4C" />
    </svg>
  );
}

export function CvGnomeLogo({ compact = false }: LogoProps) {
  return (
    <span className={`cvgnome-logo${compact ? " cvgnome-logo--compact" : ""}`}>
      <CvGnomeMark decorative />
      <span className="cvgnome-wordmark">cvgnome</span>
    </span>
  );
}
