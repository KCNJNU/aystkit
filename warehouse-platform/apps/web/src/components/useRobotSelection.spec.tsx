import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { robotQuery, selectedRobot, selectRobot } from '../services/robot-selection';
import { useRobotSelection } from './useRobotSelection';

const first = '00000000-0000-4000-8000-000000000001';
const second = '00000000-0000-4000-8000-000000000002';
const clients: QueryClient[] = [];
function Harness() {
  const { robotId, transitioning, switchRobot } = useRobotSelection();
  const location = useLocation();
  const navigate = useNavigate();
  return <>
    <output data-testid="robot">{robotId || 'all'}</output>
    <output data-testid="route">{location.pathname + location.search}</output>
    {!transitioning && <output data-testid="request">{robotQuery('/v1/files')}</output>}
    <button onClick={() => switchRobot(first)}>First</button>
    <button onClick={() => switchRobot(second)}>Second</button>
    <button onClick={() => switchRobot('')}>All</button>
    <button onClick={() => navigate('/files')}>Files</button>
    <button onClick={() => navigate(-1)}>Back</button>
    <button onClick={() => navigate(1)}>Forward</button>
  </>;
}
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/datasets?robotId=' + first]}><Harness /></MemoryRouter></QueryClientProvider>);
  return client;
}
describe('Robot router history', () => {
  beforeEach(() => { sessionStorage.clear(); selectRobot(first); });
  afterEach(() => clients.splice(0).forEach(client => client.clear()));
  it('restores the selected robot and request context on Back and Forward, including All', async () => {
    mount();
    expect(screen.getByTestId('request')).toHaveTextContent('robotId=' + first);
    fireEvent.click(screen.getByText('Second'));
    await waitFor(() => expect(screen.getByTestId('robot')).toHaveTextContent(second));
    expect(screen.getByTestId('request')).toHaveTextContent('robotId=' + second);
    fireEvent.click(screen.getByText('All'));
    await waitFor(() => expect(screen.getByTestId('route')).toHaveTextContent('robotId=all'));
    await waitFor(() => expect(screen.getByTestId('request')).toHaveTextContent(/^\/v1\/files$/));
    fireEvent.click(screen.getByText('Back'));
    await waitFor(() => expect(screen.getByTestId('request')).toHaveTextContent('robotId=' + second));
    expect(selectedRobot()).toBe(second);
    fireEvent.click(screen.getByText('Back'));
    await waitFor(() => expect(screen.getByTestId('request')).toHaveTextContent('robotId=' + first));
    expect(selectedRobot()).toBe(first);
    fireEvent.click(screen.getByText('Forward'));
    await waitFor(() => expect(screen.getByTestId('request')).toHaveTextContent('robotId=' + second));
    fireEvent.click(screen.getByText('Forward'));
    await waitFor(() => expect(screen.getByTestId('request')).toHaveTextContent(/^\/v1\/files$/));
    expect(selectedRobot()).toBe('');
  });
  it('writes the robot onto page navigation so returning to that history entry remains unambiguous', async () => {
    mount();
    fireEvent.click(screen.getByText('Files'));
    await waitFor(() => expect(screen.getByTestId('route')).toHaveTextContent('/files?robotId=' + first));
    fireEvent.click(screen.getByText('Second'));
    await waitFor(() => expect(screen.getByTestId('request')).toHaveTextContent('robotId=' + second));
    fireEvent.click(screen.getByText('Back'));
    await waitFor(() => expect(screen.getByTestId('route')).toHaveTextContent('/files?robotId=' + first));
    await waitFor(() => expect(screen.getByTestId('request')).toHaveTextContent('robotId=' + first));
  });
});
