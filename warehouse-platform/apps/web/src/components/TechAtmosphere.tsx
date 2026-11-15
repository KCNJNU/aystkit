import { useEffect, useState } from 'react';

/** Persistent decoration; respects reduced motion and pauses while the page is hidden. */
export function TechAtmosphere() {
  const [reduced, setReduced] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [visible, setVisible] = useState(() => !document.hidden);

  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onPreference = () => setReduced(media.matches);
    const onVisibility = () => setVisible(!document.hidden);
    media.addEventListener('change', onPreference);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      media.removeEventListener('change', onPreference);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  useEffect(() => {
    document.documentElement.dataset.ambientMotion = !reduced && visible ? 'running' : 'paused';
    return () => { delete document.documentElement.dataset.ambientMotion; };
  }, [reduced, visible]);

  return (
    <div className="tech-atmosphere" aria-hidden="true">
      <div className="ambient-grid" />
      <div className="ambient-orb ambient-orb-blue" />
      <div className="ambient-orb ambient-orb-violet" />
      <div className="ambient-track ambient-track-one"><span /></div>
      <div className="ambient-track ambient-track-two"><span /></div>
    </div>
  );
}
