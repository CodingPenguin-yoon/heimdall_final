import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import '@/shared/styles/tokens.css';
import '@/shared/styles/global.css';
import '@/shared/styles/components.css';
import '@/shared/styles/pages.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
