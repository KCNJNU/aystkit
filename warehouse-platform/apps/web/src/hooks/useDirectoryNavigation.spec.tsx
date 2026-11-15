import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { useDirectoryNavigation } from './useDirectoryNavigation';

function Explorer() {
  const directory = useDirectoryNavigation();
  const location = useLocation();
  const navigate = useNavigate();
  return <>
    <output data-testid="location">{location.pathname}{location.search}</output>
    <output data-testid="entry-key">{location.key}</output>
    <button disabled={!directory.canBack} onClick={directory.back}>Directory back</button>
    <button disabled={!directory.canForward} onClick={directory.forward}>Directory forward</button>
    <button onClick={() => { directory.back(); directory.back(); }}>Rapid back</button>
    <button onClick={() => navigate(`${location.pathname}?path=camera`)}>Open camera</button>
    <button onClick={() => navigate(`${location.pathname}?path=camera&page=2`)}>Next page</button>
    <button onClick={() => navigate(`${location.pathname}?path=lidar`)}>Open lidar</button>
    <button onClick={() => navigate(`${location.pathname}?path=replaced`, { replace: true })}>Replace</button>
    <button onClick={() => navigate(location.pathname + location.search)}>Repeat location</button>
    <button onClick={() => navigate('/datasets/two?path=other')}>Other dataset</button>
    <button onClick={() => navigate(-1)}>Browser back</button>
    <button onClick={() => navigate(1)}>Browser forward</button>
  </>;
}

function mount(initialEntries = ['/datasets/one'], initialIndex = initialEntries.length - 1) {
  return render(<MemoryRouter initialEntries={initialEntries} initialIndex={initialIndex}><Explorer /></MemoryRouter>);
}
const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }));
const current = () => screen.getByTestId('location').textContent;
const back = () => screen.getByRole('button', { name: 'Directory back' });
const forward = () => screen.getByRole('button', { name: 'Directory forward' });

describe('useDirectoryNavigation', () => {
  it('disables both directions on direct entry even when unrelated browser history exists', () => {
    mount(['/outside', '/datasets/one']);
    expect(back()).toBeDisabled();
    expect(forward()).toBeDisabled();
    click('Directory back');
    expect(current()).toBe('/datasets/one');
  });

  it('tracks folders and pagination and restores the exact prior URL', () => {
    mount();
    click('Open camera');
    click('Next page');
    expect(current()).toBe('/datasets/one?path=camera&page=2');
    expect(back()).toBeEnabled();
    expect(forward()).toBeDisabled();
    click('Directory back');
    expect(current()).toBe('/datasets/one?path=camera');
    expect(forward()).toBeEnabled();
    click('Directory back');
    expect(current()).toBe('/datasets/one');
    expect(back()).toBeDisabled();
    click('Directory forward');
    expect(current()).toBe('/datasets/one?path=camera');
    click('Directory forward');
    expect(current()).toBe('/datasets/one?path=camera&page=2');
    expect(forward()).toBeDisabled();
  });

  it('reflects browser POP navigation through known entries', () => {
    mount();
    click('Open camera');
    click('Next page');
    click('Browser back');
    expect(current()).toBe('/datasets/one?path=camera');
    expect(back()).toBeEnabled();
    expect(forward()).toBeEnabled();
    click('Browser forward');
    expect(current()).toBe('/datasets/one?path=camera&page=2');
    expect(forward()).toBeDisabled();
  });

  it('replaces the current entry without losing a known forward entry', () => {
    mount();
    click('Open camera');
    click('Next page');
    click('Directory back');
    click('Replace');
    expect(current()).toBe('/datasets/one?path=replaced');
    expect(forward()).toBeEnabled();
    click('Directory forward');
    expect(current()).toBe('/datasets/one?path=camera&page=2');
    click('Directory back');
    expect(current()).toBe('/datasets/one?path=replaced');
    click('Directory back');
    expect(current()).toBe('/datasets/one');
  });

  it('drops the old forward branch after a new push', () => {
    mount();
    click('Open camera');
    click('Next page');
    click('Directory back');
    click('Open lidar');
    expect(current()).toBe('/datasets/one?path=lidar');
    expect(forward()).toBeDisabled();
    click('Directory back');
    expect(current()).toBe('/datasets/one?path=camera');
    click('Directory forward');
    expect(current()).toBe('/datasets/one?path=lidar');
  });

  it('distinguishes repeated URLs by their history entry keys', () => {
    mount();
    click('Open camera');
    const firstKey = screen.getByTestId('entry-key').textContent;
    click('Repeat location');
    const repeatedKey = screen.getByTestId('entry-key').textContent;
    expect(repeatedKey).not.toBe(firstKey);
    click('Directory back');
    expect(current()).toBe('/datasets/one?path=camera');
    expect(screen.getByTestId('entry-key')).toHaveTextContent(firstKey!);
    expect(back()).toBeEnabled();
    click('Directory back');
    expect(current()).toBe('/datasets/one');
    click('Directory forward');
    click('Directory forward');
    expect(screen.getByTestId('entry-key')).toHaveTextContent(repeatedKey!);
  });

  it('resets at an unknown POP instead of crossing an unobserved boundary', () => {
    mount(['/datasets/one?path=unobserved', '/datasets/one']);
    click('Open camera');
    click('Browser back');
    expect(forward()).toBeEnabled();
    click('Browser back');
    expect(current()).toBe('/datasets/one?path=unobserved');
    expect(back()).toBeDisabled();
    expect(forward()).toBeDisabled();
    click('Browser forward');
    expect(current()).toBe('/datasets/one');
    expect(back()).toBeDisabled();
    expect(forward()).toBeDisabled();
  });

  it('never enables module history across dataset pathnames', () => {
    mount();
    click('Open camera');
    click('Other dataset');
    expect(current()).toBe('/datasets/two?path=other');
    expect(back()).toBeDisabled();
    expect(forward()).toBeDisabled();
    click('Browser back');
    expect(current()).toBe('/datasets/one?path=camera');
    expect(back()).toBeDisabled();
    expect(forward()).toBeDisabled();
  });

  it('ignores repeated traversal requests before the first location update', () => {
    mount();
    click('Open camera');
    click('Next page');
    click('Rapid back');
    expect(current()).toBe('/datasets/one?path=camera');
    expect(back()).toBeEnabled();
    click('Directory back');
    expect(current()).toBe('/datasets/one');
    expect(back()).toBeDisabled();
  });
});
