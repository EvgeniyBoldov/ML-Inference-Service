import React from 'react';

const paths = {
  grid: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>,
  model: <><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 12 9 5 9-5M3 16l9 5 9-5" /></>,
  activity: <path d="M3 12h4l3-8 4 16 3-8h4" />,
  play: <path d="m8 5 11 7-11 7V5Z" />,
  upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4" /></>,
  refresh: <><path d="M20 11a8 8 0 0 0-14-5L3 9m0-6v6h6M4 13a8 8 0 0 0 14 5l3-3m0 6v-6h-6" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  alert: <><path d="m10.3 4-8 14a1.6 1.6 0 0 0 1.4 2.4h16.6a1.6 1.6 0 0 0 1.4-2.4l-8-14a2 2 0 0 0-3.4 0Z" /><path d="M12 9v4m0 3v.1" /></>,
  check: <path d="m5 12 4 4L19 6" />,
  copy: <><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V4H4v12h4" /></>,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
};

export default function Icon({ name, className = '', size = 18 }) {
  return <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.model}</svg>;
}
