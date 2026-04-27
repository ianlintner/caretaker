import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { SWRConfig } from 'swr'
import { fetcher } from '@/lib/api'
import ThemeProvider from '@/components/ThemeProvider'
import RequireAuth from '@/components/RequireAuth'
import Login from '@/pages/Login'
import Dashboard from '@/pages/Dashboard'
import PRs from '@/pages/PRs'
import Issues from '@/pages/Issues'
import Runs from '@/pages/Runs'
import StreamRuns from '@/pages/StreamRuns'
import StreamRunDetail from '@/pages/StreamRunDetail'
import Memory from '@/pages/Memory'
import Skills from '@/pages/Skills'
import Agents from '@/pages/Agents'
import Graph from '@/pages/Graph'
import Fleet from '@/pages/Fleet'
import FleetDetail from '@/pages/FleetDetail'
import Alerts from '@/pages/Alerts'
import Config from '@/pages/Config'

export default function App() {
  return (
    <ThemeProvider>
      <SWRConfig value={{ fetcher, revalidateOnFocus: false, shouldRetryOnError: false }}>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route element={<RequireAuth />}>
              <Route path="/" element={<Dashboard />} />
              <Route path="/prs" element={<PRs />} />
              <Route path="/issues" element={<Issues />} />
              <Route path="/runs" element={<Runs />} />
              <Route path="/streams" element={<StreamRuns />} />
              <Route path="/streams/:runId" element={<StreamRunDetail />} />
              <Route path="/memory" element={<Memory />} />
              <Route path="/memory/:namespace" element={<Memory />} />
              <Route path="/skills" element={<Skills />} />
              <Route path="/agents" element={<Agents />} />
              <Route path="/graph" element={<Graph />} />
              <Route path="/fleet" element={<Fleet />} />
              <Route path="/fleet/:owner/:repo" element={<FleetDetail />} />
              <Route path="/alerts" element={<Alerts />} />
              <Route path="/config" element={<Config />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </SWRConfig>
    </ThemeProvider>
  )
}
