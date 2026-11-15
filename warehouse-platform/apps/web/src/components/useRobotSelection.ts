import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { rememberObserverRobot, robotFromSearch, robotPath, selectedRobot, selectRobot } from '../services/robot-selection';

/** Synchronize router history before mounting any robot-scoped page. */
export function useRobotSelection() {
  const location = useLocation();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [robotId, setRobotId] = useState(selectedRobot);
  const requested = robotFromSearch(location.search) ?? robotId;
  const transitioning = requested !== robotId;
  useEffect(() => {
    const observer = new URLSearchParams(location.search).get('observerRobot');
    if (observer) rememberObserverRobot(requested, observer);
    if (!transitioning) {
      const canonical = robotPath(location.pathname + location.search + location.hash, robotId);
      if (canonical !== location.pathname + location.search + location.hash) {
        navigate(canonical, { replace: true, state: location.state });
      }
      return;
    }
    let active = true;
    void client.cancelQueries({ predicate: query => query.queryKey[0] !== 'fleet-robots' }).then(() => {
      if (!active) return;
      selectRobot(requested);
      client.removeQueries({ predicate: query => !['fleet-robots', 'observer-fleet-links', 'auth'].includes(String(query.queryKey[0])) });
      setRobotId(requested);
    });
    return () => { active = false; };
  }, [client, location.hash, location.key, location.pathname, location.search, location.state, navigate, requested, robotId, transitioning]);
  return { robotId, transitioning, switchRobot: (value: string) => navigate(robotPath('/datasets', value)) };
}
