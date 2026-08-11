import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import '@/shared/styles/tokens.css';
import '@/shared/styles/global.css';
import '@/shared/styles/components.css';
import '@/shared/styles/project-pages.css';
import '@/shared/styles/deployment-detail-page.css';
import '@/shared/styles/deploy-project.css';
import '@/shared/styles/deployment-activity-page.css';
import '@/shared/styles/page-responsive.css';
import '@/shared/styles/project-configuration.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
