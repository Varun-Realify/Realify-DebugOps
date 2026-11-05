import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError } from './api'
import App from './App'
import './index.css'

const queryClient: QueryClient = new QueryClient({
  queryCache: new QueryCache({
    // Any 401 means the session ended (expired, revoked, logged out elsewhere): drop to login.
    onError: (err) => {
      if (err instanceof ApiError && err.status === 401) queryClient.setQueryData(['me'], null)
    },
  }),
  defaultOptions: { queries: { retry: (n, err) => !(err instanceof ApiError && err.status < 500) && n < 2 } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
