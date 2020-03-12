#!/usr/bin/env python
# b 1702 b 2430 b 2475
from collections import defaultdict, deque, namedtuple
import errno
import getopt
import heapq
import itertools
import logging
import math
import os
import os.path
import output
import pdb
#import pydot
import random
import re
import shutil
import signal
import sys
import traceback
import urlparse
import utils
import subprocess
import time

# Imports that we wrote
from utils import all_same, median, DebugDict, CustomDict
from running_average import RunningAverage
from randgen import RandGen
from lazyproperty import lazyproperty
from constants import Constants
from stateset import StateSet
from request import Request
from form_filler import FormFiller
from ignore_urls import filterIgnoreUrlParts
from vectors import urlvector, formvector
from form_field import FormField
from response import Response
from request_response import RequestResponse
from anchor import Anchor, AbstractAnchor
from link import Link, Links, AbstractLink
from abstract_links import AbstractLinks
from redirect import Redirect, AbstractRedirect
from form import Form, AbstractForm
from page import Page, AbstractPage
from recursive_dict import RecursiveDict
from custom_exceptions import PageMergeException, MergeLinksTreeException
from page_clusterer import PageClusterer
from abstract_request import AbstractRequest
from target import Target, PageTarget, ReqTarget, FormTarget
from abstract_map import AbstractMap
from pair_counter import PairCounter
from classifier import Classifier

import java
import com.gargoylesoftware.htmlunit as htmlunit

sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), "audit"))

print sys.path

# there are problems with the blindSqli-plugin
audit_plugins_names = ['xss', 'sqli', 'remoteFileInclude', 'osCommanding', 'localFileInclude', 'eval'] #'blindSqli']
audit_plugins = map((lambda x:(__import__(x), x)), audit_plugins_names)

from plugin_wrapper import plugin_wrapper
import knowledgeBase as kb


LAST_REQUEST_BOOST=0.1
POST_BOOST=0.2
QUERY_BOOST=0.1

# Stop exploring links with PENALTY_THRESHOLD or higher penalty
PENALTY_THRESHOLD = 3

# Size of the running verage vector for deciding if we are doing progress
RUNNING_AVG_SIZE = 10

# running htmlunit via JCC will override the signal handlers, so make sure the
# SIGINT handler is set after starting htmlunit
#htmlunit.initVM(':'.join([htmlunit.CLASSPATH, '.']), vmargs='-Djava.awt.headless=true', initialheap='2024m', maxheap='10216m')
#htmlunit.initVM(':'.join([htmlunit.CLASSPATH, '.']), vmargs='-Djava.awt.headless=true', initialheap='512m', maxheap='2024m')

wanttoexit = False

def handleError(self, record):
      raise
logging.Handler.handleError = handleError

cond = 0

maxstate = -1

PastPage = namedtuple("PastPage", "req page chlink cstate nvisits")
LinkIdx = Link.LinkIdx

class Score(object):

    def __init__(self, counter, req, dist):
        self.counter = counter
        self.req = req
        self.dist = dist
        if not req.reqresp.response.page.links:
            # XXX if a page is a dead end, assume it cannot cause a state
            # transition this could actually happen, but support for handling it
            # is not in place (i.e. we will have a request from state B, but the
            # correponding abstract link in the previous page will only have
            # state A, therefore an asswert will fail
            self.hitscore = self.boost = 0
        else:
            self.hitscore = self.pagehitscore(counter)
            self.boost = self.pagescoreboost(req, dist)
        self.score = self.hitscore + self.boost

    def __str__(self):
        return "%f(%f+%f)" % (self.score, self.hitscore, self.boost)

    def __repr__(self):
        return str(self)

    def __cmp__(self, s):
        return cmp((self.score, self.hitscore), (s.score, s.hitscore))

    @staticmethod
    def pagehitscore(counter):
        """ counter[0] is n_(l,seen) and counter[1] is n_(l,trans)  """
        if counter[0] >= 3:
            score = -(1-float(counter[1])/counter[0])**2 + 1
        else:
            score = -(1-float((counter[1]+1))/(counter[0]+1))**2 + 1
        return score

    @staticmethod
    def pagescoreboost(req, dist):
        boost = float(LAST_REQUEST_BOOST)
        if req.isPOST:
            boost += POST_BOOST
        elif req.query:
            boost += QUERY_BOOST
        boost /= dist+1
        return boost


class AppGraphGenerator(object):

    class AddToAbstractRequestException(PageMergeException):
        def __init__(self, msg=None):
            PageMergeException.__init__(self, msg)

    class AddToAppGraphException(PageMergeException):
        def __init__(self, msg=None):
            PageMergeException.__init__(self, msg)

    def __init__(self, reqrespshead, abspages, statechangescores, formfiller, crawler):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.reqrespshead = reqrespshead
        self.abspages = abspages
        self.absrequests = None
        self.statechangescores = statechangescores
        self.formfiller = formfiller
        self.crawler = crawler
        self.nstates = 0
        self.request_cluster = None

    def updatepageclusters(self, abspages):
        self.abspages = abspages


    def clusterRequests(self):
        # We're going to cluster the pages based on a pre-fix tree of their signatures.
        # We'll use the same thing as the old algorithm: only cluster if one of the requests
        # is a superset of all the others.
        
        self.request_cluster = Classifier(lambda request: request.signature_vector)
        for rr in self.reqrespshead:
            rr.request.absrequest = None
            self.request_cluster.add(rr.request)

        self.mark_clusterable(self.request_cluster)

        abstract_requests = self.create_abstract_requests(self.request_cluster, [])
        self.absrequests = set(abstract_requests)

        
    def get_abstract_request_or_create(self, request):
        abs_request = None
        if self.request_cluster.is_present(request):
            same_reqs = self.request_cluster.get_object(request)
            abs_request = same_reqs[0].absrequest
        else:
            # haven't made this exact request yet, so create a new abstract request
            self.request_cluster.add(request)
            abs_request = AbstractRequest(request)
            request.absrequest = abs_request
        return abs_request
        

    def create_abstract_requests(self, level, prev):
        to_return = list()
        for k, v in level.iteritems():
            if v.clusterable:
                requests = reduce(lambda x,y: x+y, [i for i in v.iterleaves()])
                abs_request = AbstractRequest(requests[0])
                for request in requests:
                    abs_request.reqresps.append(request.reqresp)
                    request.absrequest = abs_request
                    
                to_return.append(abs_request)
            else:
                if v.value:
                    # Not clusterable, but there are requests at this level, so cluster them.
                    requests = v.value
                    abs_request = AbstractRequest(requests[0])
                    for request in requests:
                        abs_request.reqresps.append(request.reqresp)
                        request.absrequest = abs_request
                    to_return.append(abs_request)
                to_return.extend(self.create_abstract_requests(v, prev))
        return prev + to_return

    def mark_clusterable(self, level):
        for k, v in level.iteritems():
            requests = [i for i in v.iterleaves()]
            if self.requests_are_clusterable(requests):
                v.clusterable = True
            else:
                v.clusterable = False
                self.mark_clusterable(v)

    def requests_are_clusterable(self, requests):
        
        abs_pages = map(lambda reqs: [r.reqresp.response.page.abspage for r in reqs], requests)
        
        unique_abs_pages = map(frozenset, abs_pages)

        all_abs_pages = reduce(lambda x,y: x.union(y), unique_abs_pages, set())
        
        max_unique_abs_pages_len = max(len(i) for i in unique_abs_pages)

        return len(all_abs_pages) == max_unique_abs_pages_len
        
    def addtorequestclusters(self, rr):
        abs_request = self.get_abstract_request_or_create(rr.request)
        if abs_request.request_actually_made():
            # if the abstract request that this maps to has the same abstract page, then add it to the 
            # abstract request. Otherwise redo clustering.

            all_abs_pages_in_abs_request = frozenset(r.response.page.abspage for r in abs_request.reqresps)
            if not rr.response.page.abspage in all_abs_pages_in_abs_request:
                raise AppGraphGenerator.AddToAbstractRequestException()

        abs_request.reqresps.append(rr)
        rr.request.absrequest = abs_request
        self.absrequests.add(abs_request)

    def generateAppGraph(self):

        # make sure we are at the beginning
        assert self.reqrespshead.prev is None

        curr = self.reqrespshead
        laststate = 0

        self.clusterRequests()

        currabsreq = curr.request.absrequest
        self.headabsreq = currabsreq


        # go through the while navigation path and link together AbstractRequests and AbstractPages
        # for now, every request will generate a new state, post processing will happen late
        cnt = 0
        while curr:
            currpage = curr.response.page
            currabspage = currpage.abspage
            assert not laststate in currabsreq.targets

            currabsreq.targets[laststate] = PageTarget(currabspage, laststate+1, nvisits=1)
            currpage.reqresp.request.state = laststate

            laststate += 1

            assert currpage.state == -1 or currpage.state == laststate
            currpage.state = laststate

            # keep track of the RequestResponse object per state
            assert not currabspage.statereqrespsmap[laststate]
            currabspage.statereqrespsmap[laststate] = [curr]

            if curr.next:
                assert curr.next.backto == None, "This should never be true now that we removed going back."
                if curr.next.backto is not None:
                    currpage = curr.next.backto.response.page
                    currabspage = currpage.abspage
                # find which link goes to the next request in the history
                chosenlink = (i for i, l in currpage.links.iteritems() if curr.next in l.to).next()
                nextabsreq = curr.next.request.absrequest
                assert not laststate in currabspage.abslinks[chosenlink].targets
                newtgt = ReqTarget(nextabsreq, laststate, nvisits=1)
                if chosenlink.type == Links.Type.FORM:
                    chosenlink = LinkIdx(chosenlink.type, chosenlink.path,
                            curr.next.request.formparams)
                    ## TODO: for now assume that different FORM requests are not clustered
                    # we should get the form parameters from the chosenlink, 
                    assert chosenlink.params != None
                    tgtdict = {chosenlink.params: newtgt}
                    newtgt = FormTarget(tgtdict, laststate, nvisits=1)
                chosenlinktargets = currabspage.abslinks[chosenlink].targets
                assert laststate not in chosenlinktargets
                chosenlinktargets[laststate] = newtgt
                assert not laststate in currabspage.statelinkmap
                tgt = newtgt
                if isinstance(tgt, FormTarget):
                    currabspage.statelinkmap[laststate] = currabspage.abslinks[chosenlink].targets[laststate].target[chosenlink.params]
                else:
                    currabspage.statelinkmap[laststate] = currabspage.abslinks[chosenlink].targets[laststate]
            curr = curr.next
            currabsreq = nextabsreq
            cnt += 1

        self.maxstate = laststate

        return laststate

    def addtoAppGraph(self, reqresp, state):
        self.addtorequestclusters(reqresp)

        curr = reqresp.prev
        currpage = curr.response.page
        currabspage = currpage.abspage

        if curr.next.backto is not None:
            currpage = curr.next.backto.response.page
            currabspage = currpage.abspage

        # find which link goes to the next request in the history
        chosenlinkidx = (i for i, l in currpage.links.iteritems() if curr.next in l.to).next()
        chosenlink = currabspage.abslinks[chosenlinkidx]
        nextabsreq = curr.next.request.absrequest

        if state in chosenlink.targets:
            tgt = chosenlink.targets[state]
            if tgt.nvisits:
                if tgt.target != nextabsreq:
                    # cannot map the page to the current state, need to redo clustering
                    raise AppGraphGenerator.AddToAppGraphException("%s != %s [1]" % (tgt.target, nextabsreq))
                tgt.nvisits += 1
            else:
                chosenlink.targets[state] = ReqTarget(nextabsreq, state, nvisits=1)
        else:
            chosenlink.targets[state] = ReqTarget(nextabsreq, state, nvisits=1)


        curr = curr.next

        currabsreq = nextabsreq
        currpage = curr.response.page
        assert currpage == reqresp.response.page
        currabspage = currpage.abspage

        if state in currabsreq.targets:
            tgt = currabsreq.targets[state]
            if tgt.target != currabspage:
                # cannot map the page to the current state, need to redo clustering
                raise AppGraphGenerator.AddToAppGraphException("%s != %s [2]" % (tgt.target, currabspage))
            tgt.nvisits += 1
        else:
            # if this request is know to change state changes, do not propagate the current state, but recluster
            smallerstates = [(s, t) for s, t in currabsreq.targets.iteritems()
                    if s < state]
            if smallerstates and any(t.transition != s for s, t in smallerstates):
                raise AppGraphGenerator.AddToAppGraphException("new state from %d %s" % (state, currabspage))
            tgt = PageTarget(currabspage, state, nvisits=1)
            currabsreq.targets[state] = tgt

        currabspage.statereqrespsmap[state].append(curr)


        return tgt.transition


    def updateSeenStates(self):
        for ar in self.absrequests:
            for t in ar.targets.itervalues():
                t.target.seenstates.add(t.transition)

        # Removing the following assert, as if fails for the new abstract requests
        for ap in self.abspages:
            allstates = set(s for l in ap.abslinks for s in l.targets)
            # allstates empty when we step back from a page
            # allstates has one element less than seenstates when it is the last page seen
            assert not allstates or (len(ap.seenstates - allstates) <= 1 \
                    and not allstates - ap.seenstates), \
                    "%s\n\t%s\n\t%s" % (ap, sorted(ap.seenstates), sorted(allstates))

    def updateSeenStatesForPage(self, reqresp):
        ar = reqresp.request.absrequest
        for t in ar.targets.itervalues():
            if t.nvisits > 0:
                t.target.seenstates.add(t.transition)

        ap = reqresp.response.page.abspage
        allstates = set(s for l in ap.abslinks for s in l.targets)
        # allstates empty when we step back from a page
        # allstates has one element less than seenstates when it is the last page seen
        assert not allstates or (len(ap.seenstates - allstates) <= 1 \
                and not allstates - ap.seenstates), \
                "%s\n\t%s\n\t%s" % (ap, sorted(ap.seenstates), sorted(allstates))

    def fillMissingRequests(self):

        self.updateSeenStates()

        for ap in self.abspages:
            self.fillPageMissingRequests(ap)

        all_requests = set(reduce(lambda x,y: x+y, [i for i in self.request_cluster.iterleaves()]))
        self.allabsrequests = set(i.absrequest for i in all_requests)
        self.allabsrequests |= self.absrequests
        self.absrequests = self.allabsrequests


    def fillMissingRequestsForPage(self, reqresp):

        self.updateSeenStatesForPage(reqresp)

        self.fillMissingRequests()


    def fillPageMissingRequests(self, ap):
        allstates = ap.seenstates
        for idx, l in ap.abslinks.iteritems():
            newrequest = None  # abstract request for link l
            newrequestbuilt = False # attempt has already been made to build an
                                    # abstract request
            for s in sorted(allstates):
                if s not in l.targets:
                    if isinstance(l, AbstractForm):
                        if not newrequestbuilt:
                            newwebrequests, requestparams = \
                                    self.crawler.getNewFormRequests(
                                    ap.reqresps[0].response.page,
                                    idx, l, self.formfiller)
                            if newwebrequests:
                                requests = [Request(i) for i in newwebrequests]
                                newrequest = [
                                        self.get_abstract_request_or_create(i)
                                        for i in requests]
                                assert all(i for i in newrequest)
                            newrequestbuilt = True
                        if newrequest:
                            newtgts = [ReqTarget(i, transition=s, nvisits=0)
                                    for i in newrequest]
                            tgtdict = dict((rp, ar) for rp, ar 
                                    in zip(requestparams, newtgts))
                            newtgt = FormTarget(tgtdict, s, nvisits=0)
                            l.targets[s] = newtgt
                    else:
                        if not newrequestbuilt:
                            newwebrequest = self.crawler.getNewRequest(
                                    ap.reqresps[0].response.page, idx, l)
                            if newwebrequest:
                                request = Request(newwebrequest)
                                newrequest = self.get_abstract_request_or_create(request)
                                assert newrequest
                            newrequestbuilt = True
                        if newrequest:
                            newtgt = ReqTarget(newrequest, transition=s, nvisits=0)
                            l.targets[s] = newtgt
                else:
                    assert l.targets[s].target is not None

    def getMinMappedState(self, state, statemap):
        prev = state
        mapped = statemap[state]
        while mapped != prev:
            prev = mapped
            mapped = statemap[mapped]
        return mapped

    def addStateBins(self, statebins, equalstates):
        seenstates = set(itertools.chain.from_iterable(statebins))
        newequalstates = set()
        for ss in statebins:
            otherstates = seenstates - ss
            for es in equalstates:
                newes = StateSet(es-otherstates)
                if newes:
                    newequalstates.add(newes)
        return newequalstates

    def markDifferentStates(self, seentogether, differentpairs):
       for ar in sorted(self.absrequests):
            diffbins = defaultdict(set)
            equalbins = defaultdict(set)
            for s, t in ar.targets.iteritems():
                diffbins[t.target].add(s)
                # these quality-only bins are usful in the case the last examined pages caused a state transition:
                # we need the latest state to appear in at least one bin!
                equalbins[t.target].add(t.transition)
            statebins = [StateSet(i) for i in diffbins.itervalues()]
            equalstatebins = [StateSet(i) for i in equalbins.itervalues()]

            for sb in statebins:
                seentogether.addset(sb)
            for esb in equalstatebins:
                seentogether.addset(esb)

            differentpairs.addallcombinations(statebins)

        # mark as different states that have seen different concreate pages in the same abstract page
       for ap in self.abspages:
            statelist = defaultdict(list)
            for s, rrs in ap.statereqrespsmap.iteritems():
                for rr in rrs:
                    statelist[rr.response.page.linksvector].append(s)

            if len(statelist) > 1:
                #differentpairs.addallcombinations(statelist.values())
                pass


    def propagateDifferestTargetStates(self, differentpairs):
        # XXX HOTSPOT
        for ar in sorted(self.absrequests):
            targetbins = defaultdict(set)
            targetstatebins = defaultdict(set)
            for s, t in ar.targets.iteritems():
                targetbins[t.target].add(t.transition)
                targetstatebins[(t.target, t.transition)].add(s)

            for t, states in sorted(targetbins.items()):
                if len(states) > 1:
                    statelist = sorted(states)

                    for i, a in enumerate(statelist):
                        for b in statelist[i+1:]:
                            if differentpairs.get(a, b):
                                differentpairs.addallcombinations((targetstatebins[(t, a)], targetstatebins[(t, b)]))

    def assignColor(self, assignments, edges, node, maxused, statereqrespmap):
        # Assign to node "node" the highest color not already assigned to one of his neighbors
        neighs = [(n, assignments[n]) for n in edges[node] if n in assignments]
        neighcolors = frozenset(n[1] for n in neighs)
        tostate = -1
        if node != 0:
            rr = statereqrespmap[node]
            ar = rr.request.absrequest
            assert node not in ar.colorassignstatepagemap
            assignedfrom = assignments[rr.request.reducedstate]
            if assignedfrom in ar.colorassignstatepagemap:
                # check if the abstract request that lead to this state (node)
                # has already marked to lead to some abstract page (abspage) in
                # an already colored state (tostate)
                toabspage, tostate = ar.colorassignstatepagemap[assignedfrom]
                assert toabspage == rr.response.page.abspage
                if tostate not in neighcolors:
                    # pick the tostate color directly, as it is the only one
                    # which will not generate a new state split
                    assert node == rr.response.page.reducedstate
                    assignments[node] = tostate
                    return maxused

        for i in range(maxused, -1, -1) + [maxused+1]:
            if i not in neighcolors:
                assignments[node] = i
                if node != 0:
                    ar.colorassignstatepagemap[assignedfrom] = (rr.response.page.abspage, i)
                maxused = max(maxused, i)
                break
            else:
                pass
        else:
            assert False
        return maxused


    def colorStateGraph(self, differentpairs, allstates, statereqrespmap):
        # XXX HOTSPOT

        # allstates should be sorted
        assert allstates[0] == 0

        edges = defaultdict(set)
        for a, b in differentpairs:
            assert a != b
            edges[a].add(b)
            edges[b].add(a)

        assignments = {}

        maxused = 0

        for ar in self.absrequests:
            ar.colorassignstatepagemap = {}
        for node in allstates:
            assert node not in assignments
            maxused = self.assignColor(assignments, edges, node, maxused, statereqrespmap)

        return assignments

    def makeset(self, statemap):
        allstatesset = frozenset(statemap)
        allstates = sorted(allstatesset)
        return allstates

    def addtodiffparis(self, allstates, seentogether, differentpairs, exceptions=frozenset()):
        for i, a in enumerate(allstates):
            for b in allstates[i+1:]:
                if a not in exceptions and b not in exceptions and not seentogether.containsSorted(a, b):
                    differentpairs.addSorted(a, b)

    def markNeverSeenTogether(self, statemap, seentogether, differentpairs, exceptions=frozenset()):

        allstates = self.makeset(statemap)

        self.addtodiffparis(allstates, seentogether, differentpairs, exceptions)

        return allstates

    def createColorBins(self, lenallstates, assignments):
            bins = [[] for i in range(lenallstates)]
            for n, c in assignments.iteritems():
                bins[c].append(n)
            bins = [StateSet(i) for i in bins if i]

            return bins

    def updateStatemapFromColorBins(self, bins, assignments, statemap):
            colormap = [min(nn) for nn in bins]

            for n, c in assignments.iteritems():
                statemap[n] = colormap[c]

    def refreshStatemap(self, statemap):
        for i in range(len(statemap)):
            statemap[i] = statemap[statemap[i]]

        nstates = len(set(statemap))
        return nstates

    def mergeStatesGreedyColoring(self, statemap, statereqrespmap):

        seentogether = PairCounter()
        differentpairs = PairCounter()

        self.markDifferentStates(seentogether, differentpairs)

        allstates = self.markNeverSeenTogether(statemap, seentogether, differentpairs, exceptions=frozenset([statemap[-1]]))
        lenallstates = len(allstates)

        olddifferentpairslen = -1

        while True:
            self.propagateDifferestTargetStates(differentpairs)
            differentpairslen = len(differentpairs)

            assert differentpairslen >= olddifferentpairslen
            if differentpairslen == olddifferentpairslen:
                break
            else:
                olddifferentpairslen = differentpairslen
            #writeColorableStateGraph(allstates, differentpairs)
            assignments = self.colorStateGraph(differentpairs, allstates, statereqrespmap)
            bins = self.createColorBins(lenallstates, assignments)
            self.updateStatemapFromColorBins(bins, assignments, statemap)
            differentpairs.addallcombinations(bins)


        self.nstates = self.refreshStatemap(statemap)



    def reqstatechangescore(self, absreq, dist):
        scores = [[(self.pagescore(i, rr.request, dist),
                i, rr.request)
            for i in self.statechangescores.getpathnleaves([rr.request.method] + list(rr.request.urlvector))]
            for rr in absreq.reqresps]

        # LUDO XXX: Another bug here, max(max(scores)) doesn't work correctly
        return max(max(scores))[0]

    def pagescore(self, counter, req, dist):
        score = Score(counter, req, dist)
        return score


    def splitStatesIfNeeded(self, smallerstates, currreq, currstate, currtarget, statemap, history):
        currmapsto = self.getMinMappedState(currstate, statemap)
        cttransition = self.getMinMappedState(currtarget.transition, statemap)
        for ss in smallerstates:
            ssmapsto = self.getMinMappedState(ss, statemap)
            if ssmapsto == currmapsto:
                sstarget = currreq.targets[ss]
                ssttransition = self.getMinMappedState(sstarget.transition, statemap)
                if sstarget.target == currtarget.target:
                    assert len(sstarget.target.statereqrespsmap[sstarget.transition]) == 1
                    assert len(currtarget.target.statereqrespsmap[currtarget.transition]) == 1
                    continue
                # mark this request as givin hints for state change detection
                currreq.statehints += 1
                pastpages = []
                for (j, (req, page, chlink, laststate)) in enumerate(reversed(history)):
                    if req == currreq or \
                            self.getMinMappedState(laststate, statemap) != currmapsto:
                        scores = [(self.reqstatechangescore(pp.req, i), pp)
                            for i, pp in enumerate(pastpages)]
                        bestcand = max(scores)[1]

                        target = bestcand.req.targets[bestcand.cstate]

                        #self.logger.debug(output.teal("splitting on best candidate %d->%d request %s to page %s"), bestcand.cstate, target.transition, bestcand.req, bestcand.page)
                        assert statemap[target.transition] == bestcand.cstate
                        # this request will always change state
                        for t in bestcand.req.targets.itervalues():
                            if t.transition <= currstate:
                                statemap[t.transition] = t.transition
                        # XXX: Could probably just return from the function here
                        break
                    # no longer using PastPage.nvisits
                    pastpages.append(PastPage(req, page, chlink, laststate, None))
                else:
                    # if we get here, we need a better heuristic for splitting state
                    raise RuntimeError()
                currmapsto = self.getMinMappedState(currstate, statemap)
                assert ssmapsto != currmapsto, "%d == %d" % (ssmapsto, currmapsto)

    def minimizeStatemap(self, statemap):
        for i in range(len(statemap)):
            statemap[i] = self.getMinMappedState(i, statemap)

    def reduceStates(self):

        # map each state to its equivalent one
        statemap = range(self.maxstate+1)
        lenstatemap = len(statemap)

        currreq = self.headabsreq

        # need history to handle navigatin back; pair of (absrequest,absresponse)
        history = []
        currstate = 0
        tgtchosenlink = None

        while True:
            currtarget = currreq.targets[currstate]

            # if any of the previous states leading to the same target caused a state transition,
            # directly guess that this request will cause a state transition
            # this behavior is needed because it might happen that the state transition is not detected,
            # and the state assignment fails
            smallerstates = [(s, t) for s, t in currreq.targets.iteritems()
                    if s < currstate and t.target == currtarget.target]
            if smallerstates and any(statemap[t.transition] != s for s, t in smallerstates):
                currstate += 1
            else:
                # find if there are other states that we have already processed that lead to a different target
                smallerstates = sorted([i for i, t in currreq.targets.iteritems() if i < currstate], reverse=True)
                if smallerstates:
                    self.splitStatesIfNeeded(smallerstates, currreq, currstate, currtarget, statemap, history)
                currstate += 1
                statemap[currstate] = currstate-1

            respage = currtarget.target

            history.append((currreq, respage, tgtchosenlink, currstate-1))
            if currstate not in respage.statelinkmap:
                if currstate == self.maxstate:
                    # end reached
                    break
                off = 0
                while currstate not in respage.statelinkmap:
                    off -= 1
                    respage = history[off][1]

            tgtchosenlink = respage.statelinkmap[currstate]
            chosentarget = tgtchosenlink.target

            currreq = chosentarget

        self.minimizeStatemap(statemap)

        self.nstates = lenstatemap


        self.collapseGraph(statemap)

        self.mergeStateReqRespMaps(statemap)

        statereqrespmap = self.assignReducedStateCreateStateReqRespMap(statemap)

        self.mergeStatesGreedyColoring(statemap, statereqrespmap)

        self.collapseGraph(statemap)

        self.mergeStateReqRespMaps(statemap)

        self.assignReducedState(statemap)

        # return last current state
        return statemap[-1]

    def assignReducedState(self, statemap):
        rr = self.reqrespshead
        while rr:
            assert rr.request.state != -1
            assert rr.response.page.state != -1
            rr.request.reducedstate = statemap[rr.request.state]
            rr.response.page.reducedstate = statemap[rr.response.page.state]
            rr = rr.next

    def assignReducedStateCreateStateReqRespMap(self, statemap):
        rr = self.reqrespshead
        statereqrespmap = {}
        prevstate = -1
        while rr:
            assert rr.request.state != -1
            assert rr.response.page.state != -1
            rr.request.reducedstate = statemap[rr.request.state]
            rr.response.page.reducedstate = statemap[rr.response.page.state]
            if rr.request.reducedstate != rr.response.page.reducedstate:
                assert rr.response.page.reducedstate not in statereqrespmap
                statereqrespmap[rr.response.page.reducedstate] = rr
            rr = rr.next
        return statereqrespmap

    def mergeStateReqRespMaps(self, statemap):
        # update the state to ReqResp map for AbstractPages
        for ap in self.abspages:
            newstatereqrespmap = defaultdict(list)
            for s, p in ap.statereqrespsmap.iteritems():
                # warning: unsorted
                newstatereqrespmap[statemap[s]].extend(p)
            ap.statereqrespsmap = newstatereqrespmap

    def collapseNode(self, nodes, statemap):
        for aa in nodes:
            statereduce = [(st, statemap[st]) for st in aa.targets]
            for st, goodst in statereduce:
                if st == goodst:
                    aa.targets[goodst].transition = statemap[aa.targets[goodst].transition]
                    if isinstance(aa, AbstractForm):
                        intgt = aa.targets[goodst].target
                        for rt in intgt.itervalues():
                            assert isinstance(rt, ReqTarget)
                            rt.transition = statemap[rt.transition]
                else:
                    if goodst in aa.targets:
                        if isinstance(aa, AbstractForm):
                            common = frozenset(aa.targets[st].target.keys()) \
                                    & frozenset(aa.targets[goodst].target.keys())
                            for c in common:
                                assert st == goodst or \
                                        statemap[aa.targets[goodst].target[c].transition] \
                                        == statemap[aa.targets[st].target[c].transition], \
                                        "%s\n\t%d->%s (%d)\n\t%d->%s (%d)" \
                                        % (aa, st, aa.targets[st], statemap[aa.targets[st].transition],
                                                goodst, aa.targets[goodst],
                                                statemap[aa.targets[goodst].transition])
                            aa.targets[goodst].nvisits += aa.targets[st].nvisits
                            aa.targets[goodst].target.update(aa.targets[st].target)
                            # in this case, as we are merging the ReqTargets in the FormTarget
                            # we need to update the ReqTarget.transition fields
                            intgt = aa.targets[goodst].target
                            for rt in intgt.itervalues():
                                assert isinstance(rt, ReqTarget)
                                rt.transition = statemap[rt.transition]

                        else:
                            assert st == goodst or \
                                    statemap[aa.targets[goodst].transition] == \
                                    statemap[aa.targets[st].transition], \
                                    "%s\n\t%d->%s (%d)\n\t%d->%s (%d)" \
                                    % (aa, st, aa.targets[st], statemap[aa.targets[st].transition],
                                            goodst, aa.targets[goodst],
                                            statemap[aa.targets[goodst].transition])
                            aa.targets[goodst].nvisits += aa.targets[st].nvisits
                    else:
                        aa.targets[goodst] = aa.targets[st]
                        # also map transition state to the reduced one
                        aa.targets[goodst].transition = statemap[aa.targets[goodst].transition]
                        if isinstance(aa, AbstractForm):
                            intgt = aa.targets[goodst].target
                            for rt in intgt.itervalues():
                                assert isinstance(rt, ReqTarget)
                                rt.transition = statemap[rt.transition]
                    del aa.targets[st]

    def collapseGraph(self, statemap):

        # merge states that were reduced to the same one
        for ap in self.abspages:
            self.collapseNode(ap.abslinks.itervalues(), statemap)

        self.collapseNode(self.absrequests, statemap)


    def markChangingState(self):
        for ar in sorted(self.absrequests):
            changing = any(s != t.transition for s, t in ar.targets.iteritems())
            if changing:
                for rr in ar.reqresps:
                    rr.request.changingstate = True


class DeferringRefreshHandler(htmlunit.ImmediateRefreshHandler):

    def __init__(self, refresh_urls=[]):
        super(DeferringRefreshHandler, self).__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.refresh_urls = refresh_urls

    def handleRefresh(self, page, url, seconds):
        pass

class Crawler(object):

    class EmptyHistory(Exception):
        pass

    class ActionFailure(Exception):
        pass

    class UnsubmittableForm(ActionFailure):
        pass

    def __init__(self, dumpdir):
        self.logger = logging.getLogger(self.__class__.__name__)
        # where to dump HTTP requests and responses
        self.dumpdir = dumpdir

        self.initial_url = None

        self.webclient = htmlunit.WebClient()

        # new syntax for htmlunit 2.36
        options = htmlunit.WebClient.getOptions(self.webclient)

        # seems to work as well as
        # options = self.webclient.getOptions()

        options.setThrowExceptionOnScriptError(False)

        options.setThrowExceptionOnFailingStatusCode(False)
        options.setUseInsecureSSL(True)
        options.setRedirectEnabled(False)

        # optional addition for silencing CSS errors and ignoring JavaScript:
        # self.webclient.setCssErrorHandler(htmlunit.SilentCssErrorHandler())
        # options.setJavaScriptEnabled(False)

        self.refresh_urls = []
        self.webclient.setRefreshHandler(DeferringRefreshHandler(self.refresh_urls))
        # last generated RequestResponse object
        self.lastreqresp = None
        # current RequestResponse object (will differ from lastreqresp when back() is invoked)
        self.currreqresp = None
        # first RequestResponse object
        self.headreqresp = None

    def dumpReqResp(self, reqresp):
        assert self.dumpdir
        basedir = "%s/%d" % (self.dumpdir, reqresp.instance)
        os.mkdir(basedir)
        f = open("%s/request" % basedir, "w")
        f.write(reqresp.request.dump.encode('utf8'))
        f.close()
        f = open("%s/response" % basedir, "w")
        f.write(reqresp.response.content.encode('utf8'))
        f.close()
        if reqresp.response.page.isHtmlPage:
            f = open("%s/page" % basedir, "w")
            f.write(reqresp.response.page.content.encode('utf8'))
            f.close()

    def open(self, url):
        del self.refresh_urls[:]
        htmlpage = self.webclient.getPage(url)
        # TODO: handle HTTP redirects, they will throw an exception
        return self.newPage(htmlpage)

    def followRedirect(self, redirect):
        lastvalidpage = self.currreqresp.response.page
        reqresp = None
        while lastvalidpage.redirect or lastvalidpage.error:
            if not lastvalidpage.reqresp.prev:
                # beginning of history
                lastvalidpage = None
                break
            lastvalidpage = lastvalidpage.reqresp.prev.response.page
        if lastvalidpage:
            fqurl = lastvalidpage.internal.getFullyQualifiedUrl(redirect.location)
        else:
            fqurl = redirect.location

        page = self.webclient.getPage(fqurl)
        reqresp = self.reqresp_from_page(page)

        redirect.to.append(reqresp)
        return reqresp

    def newPage(self, htmlpage):
        page = Page(htmlpage, initial_url=self.initial_url, webclient=self.webclient)
        webresponse = htmlpage.getWebResponse()
        response = Response(webresponse, page=page)
        request = Request(webresponse.getWebRequest())
        reqresp = RequestResponse(request, response)
        if self.dumpdir:
            self.dumpReqResp(reqresp)
        request.reqresp = reqresp
        page.reqresp = reqresp

        self.updateInternalData(reqresp)
        return self.currreqresp

    def newHttpRedirect(self, webresponse):
        redirect = Page(webresponse, redirect=True, initial_url=self.initial_url, webclient=self.webclient)
        response = Response(webresponse, page=redirect)
        request = Request(webresponse.getWebRequest())
        reqresp = RequestResponse(request, response)
        if self.dumpdir:
            self.dumpReqResp(reqresp)
        request.reqresp = reqresp
        redirect.reqresp = reqresp

        self.updateInternalData(reqresp)
        return self.currreqresp

    def newHttpError(self, webresponse):
        error = Page(webresponse, error=True, initial_url=self.initial_url, webclient=self.webclient)
        response = Response(webresponse, page=error)
        request = Request(webresponse.getWebRequest())
        reqresp = RequestResponse(request, response)
        if self.dumpdir:
            self.dumpReqResp(reqresp)
        request.reqresp = reqresp
        error.reqresp = reqresp

        self.updateInternalData(reqresp)
        return self.currreqresp

    def updateInternalData(self, reqresp):
        backto = self.currreqresp if self.lastreqresp != self.currreqresp else None
        reqresp.prev = self.lastreqresp
        reqresp.backto = backto
        if self.lastreqresp is not None:
            self.lastreqresp.next = reqresp
        self.lastreqresp = reqresp
        self.currreqresp = reqresp
        if self.headreqresp is None:
            self.headreqresp = reqresp

    def handleNavigationException(self, e):
        if isinstance(e, TypeError):
            response = e.args[0].webResponse
            self.logger.info(output.purple("unexpected page"))
            reqresp = self.newHttpError(response)
        else:
            javaex = e
            if isinstance(javaex, htmlunit.FailingHttpStatusCodeException):
                httpex = javaex
                strhttpex = filterIgnoreUrlParts(str(httpex))
                self.logger.info("%s" % strhttpex)
                statuscode = httpex.getStatusCode()
                message = httpex.getMessage()
                if statuscode == 303 or statuscode == 302:
                    response = httpex.getResponse()
                    location = response.getResponseHeaderValue("Location")
                    location = filterIgnoreUrlParts(location)
                    message = filterIgnoreUrlParts(message)
                    self.logger.info(output.purple("redirect to %s %d (%s)" % (location, statuscode, message)))
                    reqresp = self.newHttpRedirect(response)
                elif statuscode == 404 or statuscode == 500 or statuscode == 403:
                    response = httpex.getResponse()
                    self.logger.info(output.purple("error %d (%s)" % (statuscode, message)))
                    reqresp = self.newHttpError(response)
                else:
                    raise
            else:
                raise
        return reqresp


    def reqresp_from_page(self, page):
        webresponse = page.getWebResponse()

        if isinstance(page, htmlunit.html.HtmlPage):
            reqresp = self.newPage(page)
        else:
            response_code = webresponse.getStatusCode()
            if response_code == 303 or response_code == 302:
                reqresp = self.newHttpRedirect(webresponse)
            else:
                reqresp = self.newHttpError(webresponse)

        return reqresp

    def click(self, anchor):

        generic_page = anchor.click()
        reqresp = self.reqresp_from_page(generic_page)
        anchor.to.append(reqresp)
        return reqresp


    @staticmethod
    def fillForm(iform, params, logger):
        # TODO add proper support for on* and target=
        attrs = list(iform.getAttributesMap().keySet())
        for a in attrs:
            if a.startswith("on") or a == "target":
                iform.removeAttribute(a)

        for k, vv in params.iteritems():
            for i, v in zip(iform.getInputsByName(k), vv):
                if isinstance(i, htmlunit.html.HtmlCheckBoxInput):
                    if v:
                        # XXX if we really care, we should move the value of input checkboxes into the key name
                        #assert i.getValueAttribute() == v
                        i.setChecked(True)
                    else:
                        i.setChecked(False)
                elif isinstance(i, htmlunit.html.HtmlSubmitInput):
                    pass
                else:
                    i.setValueAttribute(v)

            for i, v in zip(iform.getTextAreasByName(k), vv):
                textarea = i
                textarea.setText(v)


    @staticmethod
    def getInternalSubmitter(iform, submitter):
        isubmitters = list(i for i in iform.getElementsByAttribute(submitter.tag, "type", submitter.type.lower())) + list(i for i in iform.getLostChildren())
        if len(isubmitters) == 0:
            return None
        if len(isubmitters) == 1:
            isubmitter = isubmitters[0]
        else:
            assert submitter.name or submitter.value
            isubmitter = None
            for i in isubmitters:
                if (submitter.name and i.getAttribute("name").encode('ascii', 'ignore') == submitter.name) \
                        or (not submitter.name and
                            i.getAttribute("value").encode('ascii', 'ignore') == submitter.value):
                    assert isubmitter is None
                    isubmitter = i
                    break
            assert isubmitter
        return isubmitter

    def submitForm(self, form, params):
        assert isinstance(params, FormFiller.Params)

        self.logger.info(output.fuscia("submitting form %s %r and params: %r"),
                form.method.upper(), form.action,
                params)

        iform = form.internal

        self.fillForm(iform, params, self.logger)

        # TODO: explore clickable regions in input type=image
        submitter = params.submitter
        if submitter is None:
            if not form.submittables:
                self.logger.warn("no submittable item found for form %s %r",
                                 form.method,
                                 form.action)
                raise Crawler.UnsubmittableForm()
            submitter = form.submittables[0]

        isubmitter = self.getInternalSubmitter(iform, submitter)

        attrs = list(iform.getAttributesMap().keySet())

        # parameters needed to tell htmlunit to ignore visibility of button
        page = isubmitter.click(False, False, False, False, True, False)
        htmlpage = page
        if htmlpage == self.lastreqresp.response.page.internal:
            # submit did not work
            self.logger.warn("unable to submit form %s %r in page",
                             form.method,
                             form.action)
            raise Crawler.UnsubmittableForm()

        reqresp = self.reqresp_from_page(page)

        reqresp.request.formparams = params
        form.to.append(reqresp)
        act = form.action.split('?')[0]
        return reqresp

    def getNewRequest(self, page, idx, link):
        use_url = isinstance(link, AbstractRedirect) or page.error
        use_fully_qualified = isinstance(link, AbstractAnchor) and not page.error
        if use_fully_qualified:
            if len(link.hrefs) == 1:
                href = iter(link.hrefs).next()
                if not href.strip().lower().startswith("javascript:"):
                    url = page.internal.getFullyQualifiedUrl(href)
                    return htmlunit.WebRequest(url)
        elif use_url:
            href = ""
            if isinstance(link, AbstractRedirect):
                if len(link.locations) == 1:
                    href = iter(link.locations).next()
            elif isinstance(link, AbstractAnchor):
                if len(link.hrefs) == 1:
                    href = iter(link.hrefs).next()
            if href and not href.strip().lower().startswith("javascript:"):
                    url = java.net.URL(page.internal.getWebRequest().getUrl(),
                            href)
                    return htmlunit.WebRequest(url)
        assert not isinstance(link, AbstractForm)
        return None

    def getNewFormRequests(self, page, idx, link, formfiller):
        assert isinstance(link, AbstractForm)
        newrequests = []
        requestparams = []
        # XXX why??? set a breakpoint to understand
        if len(link.methods) != 1:
            pdb.set_trace()
        if len(link.methods) == 1:
            # XXX no garantee link.forms[0] is a good one
            form = link.forms[0]
            for f in formfiller.get(link.elemset, form):
                iform = page.links[idx].internal.cloneNode(True)
                self.fillForm(iform, f, self.logger)

                if f.submitter is None:
                    if not form.submittables:
                        self.logger.warn("no submittable item found for form %s %r",
                                form.method,
                                form.action)
                        raise Crawler.UnsubmittableForm()
                    submitter = form.submittables[0]
                else:
                    submitter = f.submitter

                isubmitter = Crawler.getInternalSubmitter(iform, submitter)
                if not isubmitter:
                    # couldn't find a submit button, skip creating this form.
                    continue
                newreq = iform.getWebRequest(isubmitter)
                if isinstance(isubmitter, htmlunit.html.HtmlImageInput):
                    url = newreq.getUrl()
                    urlstr = url.getPath()
                    if urlstr.find('?') == -1:
                        urlstr += "?"
                    query = url.getQuery()
                    if query:
                        urlstr += query
                        if query.endswith('&='):
                            # htmlunits generate a spurios &= at the end...
                            urlstr = urlstr[:-2]
                        urlstr += '&'
                    urlstr += "x=0&y=0"
                    newurl = java.net.URL(url, urlstr)
                    newreq.setUrl(newurl)
                newrequests.append(newreq)
                requestparams.append(f)
        return (newrequests, requestparams)

def linkweigh(link, nvisits, othernvisits=0, other_link_visits=0, statechange=False):
        statechange = 1 if statechange else 0
        if link.type == Links.Type.ANCHOR:
            if link.hasquery:
                dist = Dist((statechange, 0, 0, 0, 0, nvisits, othernvisits, other_link_visits, 0, 0, 0, 0))
            else:
                dist = Dist((statechange, 0, 0, 0, 0, 0, 0, nvisits, othernvisits, other_link_visits, 0, 0))
        elif link.type == Links.Type.FORM:
            if link.isPOST:
                dist = Dist((statechange, nvisits, othernvisits, other_link_visits, 0, 0, 0, 0, 0, 0, 0, 0))
            else:
                dist = Dist((statechange, 0, 0, nvisits, othernvisits, other_link_visits, 0, 0, 0, 0, 0, 0))
        elif link.type == Links.Type.REDIRECT:
            dist = Dist((statechange, 0, 0, 0, 0, 0, 0, 0, 0, nvisits, othernvisits, other_link_visits))
        else:
            assert False, link
        return dist

class Dist(object):
    LEN = 12

    def __init__(self, v=None):
        """ The content of the vector is as follow:
        (state changing,
         POST form, GET form, anchor w/ params, anchor w/o params, redirect
         )
        the first elelement can be either 0 or 1, whether the request proved to trigger a state change or not
        the other elements are actually doubled: one value for the number of visits in the current state, and
        one value for the number of visits in the other states
        only one pair of element can be != 0
        """
        self.val = tuple(v) if v else tuple([0]*Dist.LEN)
        assert len(self.val) == Dist.LEN

    def __add__(self, d):
        return Dist(a+b for a, b in zip(self.val, d.val))

    def __cmp__(self, d):
        return cmp(self.normalized, d.normalized)

    def __str__(self):
        return "%s[%s]" % (self.val, self.normalized)

    def __repr__(self):
        return str(self)

    def __reversed__(self):
        return reversed(self.val)

    @lazyproperty
    def normalized(self):
        penalty = sum(self.val)/5
        ret = (self.val[0], penalty, self.val[1:])
        return ret

    def __getitem__(self, i):
        return self.val[i]


class FormDB(dict):

    class Submitted(object):

        def __init__(self, values, action):
            self.values = values
            self.action = action

        def add(self, formfields, action):
            self[formfields.sortedkeys] = FormDB.Submitted(formfields, action)

class ParamDefaultDict(defaultdict):

    def __init__(self, factory):
        defaultdict.__init__(self)
        self._factory = factory

    def __missing__(self, key):
        return self._factory(key)

Candidate = namedtuple("Candidate", "priority dist path tiebreaker")
PathStep = namedtuple("PathStep", "abspage idx state")

class Engine(object):

    Actions = Constants("ANCHOR", "FORM", "REDIRECT", "DONE", "RANDOM")

    def __init__(self, formfiller=None, dumpdir=None, command_to_reset=None):
        self.requests = 0
        self.logger = logging.getLogger(self.__class__.__name__)
        self.state = -1
        self.followingpath = False
        self.pathtofollow = []
        self.formfiller = formfiller
        # where to dump HTTP requests and responses
        self.dumpdir = dumpdir
        self.running_visited_avg = RunningAverage(RUNNING_AVG_SIZE)
        self.last_ar_seen = -1
        self.since_last_ar_change = 0
        self.last_ap_pages = 1
        self.command_to_reset = command_to_reset

        self.COUNTER = 1

        # Set up the random word generator and add it to any class that uses it
        self.rng = RandGen()
        FormFiller.rng = self.rng
        Links.rng = self.rng

    def getUnvisitedLink(self, reqresp):
        page = reqresp.response.page
        abspage = page.abspage

        # if we do not have an abstract graph, pick first anchor
        # this should happen only at the first iteration
        if abspage is None:
            if len(page.anchors) > 0:
                for i, aa in page.links.iteritems():
                    if i.type == Links.Type.ANCHOR:
                        return i
                assert False
            else:
                return None

        # find unvisited anchor
        # XXX looks like the following code is never reached
        assert False
        for i, aa in abspage.abslinks.iteritems():
            if i.type == Links.Type.ANCHOR:
                if self.state not in aa.targets or aa.targets[self.state].nvisits == 0:
                    return i
        return None

    def linkcost(self, abspage, linkidx, link, state):
        statechange = False
        tgt = None
        # must be > 0, as it will also mark the kind of query in the dist vector
        nvisits = 1
        # count visits done for this link and the subsequent request the current
        # state
        if state in link.targets:
            linktarget = link.targets[state]
            nvisits += linktarget.nvisits
            tgt = linktarget.target
            # also add visit count for the subsequent request
            if isinstance(linktarget, FormTarget):
                if linkidx.params != None and linkidx.params in tgt:
                    # if we have form parameters, limits the visit count to
                    # froms submitted with that set of parameters
                    targetlist = [tgt[linkidx.params].target.targets]
                else:
                    # if we do not have form parameters, count all the submitted
                    # forms
                    targetlist = [i.target.targets for i in tgt.itervalues()]
            else:
                assert isinstance(tgt, AbstractRequest)
                targetlist = [tgt.targets]

            for targets in targetlist:
                if state in targets:
                    pagetarget = targets[state]
                    assert isinstance(pagetarget, PageTarget)
                    nvisits += pagetarget.nvisits
                    if pagetarget.transition != state:
                        statechange = True

        # must be > 0, as it will also mark the kind of query in the dist
        # vector, in case nvisits == 0
        othernvisits = 1
        if tgt:

            assert linkidx.params == None or isinstance(linktarget, FormTarget)

            # we will count in othernvisits only links leading to the following
            # set of abstract requests
            if isinstance(linktarget, FormTarget):
                finalabsreqs = frozenset(i.target for i in tgt.itervalues())
            else:
                finalabsreqs = frozenset([tgt])

            assert all(isinstance(i, AbstractRequest) for i in finalabsreqs)

            for s, t in link.targets.iteritems():
                # a link should never change state, it is the request that does it!
                assert t.transition == s

                if isinstance(t, FormTarget):
                    # add all visits made in any state with any form parameters
                    # that lead to any of the AbstractRequest found for the
                    # current state
                    for rt in t.target.itervalues():
                        assert isinstance(rt, ReqTarget)
                        # a ReqTarget should never cause a state transition
                        assert rt.transition == s
                        if rt.target in finalabsreqs:
                            othernvisits += rt.nvisits
                else:
                    assert isinstance(t.target, AbstractRequest)
                    # add all visits made in any state that lead to the same
                    # AbstractRequest
                    # we need to sum only the visits to requests that have the same target
                    # otherwise we might skip exploring them for some states
                    if t.target in finalabsreqs:
                        othernvisits += t.nvisits


            # also add visit count for the subsequent request in different states

            if linkidx.params != None and linkidx.params in tgt:
                tgt2 = [tgt[linkidx.params].target]
            elif not isinstance(linktarget, FormTarget):
                tgt2 = [tgt]
            else:
                # iterate over all AbstractRequests in ReqTargets
                tgt2 = (i.target for i in tgt.itervalues())

            for tgt3 in tgt2:
                assert isinstance(tgt3, AbstractRequest)
                for s, t in tgt3.targets.iteritems():
                    if s != state:
                        othernvisits += t.nvisits
                    # if the request has ever caused a change in state, consider it
                    # state chaning even if the state is not the same. This helps
                    # Dijstra stay away from potential state changing requests
                    if s != t.transition:
                        statechange = True

        assert link.type == linkidx.type
        # divide othernvisits by 3, so it weights less and for the first 3
        # visists it is 1
        othernvisits = int(math.ceil(othernvisits/3.0))
        
        # other_link_visits = sum(i.nvisits for i in link.targets.itervalues())
        # other_link_visits = int(math.ceil(other_link_visits/3.0))

        other_link_visits = 0

        dist = linkweigh(link, nvisits, othernvisits, other_link_visits, statechange)

        return dist

    def getCounter(self):
        self.COUNTER += 1
        return self.COUNTER

    def addUnvisited(self, dist, head, state, headpath, unvlinks, candidates, priority, new=False):
        costs = [(self.linkcost(head, i, j, state), i) for (i, j) in unvlinks]

        # Remove links with high penalty
        # XXX BUG: We probably don't want to do this if priority is -1
        costs = [i for i in costs if i[0].normalized[1] < PENALTY_THRESHOLD]
        if not costs:
            # costs might be empty after this filtering
            return
        sorted_costs = sorted(costs)
        
        # get everything that is equal to this and add it to choose one randomly
        mincosts = [sorted_costs[0]]
        mincost = self.rng.choice(mincosts)
        path = list(reversed([PathStep(head, mincost[1], state)] + headpath))
        if priority == 0:
            # for startndard priority, select unvisited links disregarding the
            # distance, provided they do not change state
            newdist = (dist[0], mincost[0], dist)
        else:
            # for non-standard priority (-1), use the real distance
            newdist = dist
        heapq.heappush(candidates, Candidate(priority, newdist, path, self.getCounter()))

    def findPathToUnvisited(self, startreqresp, startstate, recentlyseen):
        # recentlyseen is the set of requests done since last state change
        heads = [(Dist(), startreqresp.response.page.abspage, startstate, [])]
        seen = set()
        candidates = []
        while heads:
            dist, head, state, headpath = heapq.heappop(heads)
            if (head, state) in seen:
                continue
            seen.add((head, state))
            unvlinks_added = False
            for idx, link in head.abslinks.iteritems():
                if link.skip:
                    continue
                if state in link.targets:
                    linktgt = link.targets[state]
                    if isinstance(linktgt, FormTarget):
                        nextabsrequests = [(p, i.target) for p, i in linktgt.target.iteritems()]
                    else:
                        nextabsrequests = [(None, linktgt.target)]
                    for formparams, nextabsreq in nextabsrequests:
                        # Check to see if this next abstract request is the same 
                        # abstract request that was made previously. Only if its the start page

                        if nextabsreq.request_actually_made():
                            if (not headpath) and nextabsreq == startreqresp.request.absrequest:
                                continue

                        if state == startstate and nextabsreq.statehints and \
                                not nextabsreq.changingstate and \
                                not nextabsreq in recentlyseen:
                            # this is a page known to be revealing of possible state change
                            # go there first, priority=-1 !
                            # do not pass form parameters, as this is an
                            # unvisited link and we want to pick random values
                            formidx = LinkIdx(idx.type, idx.path, None)
                            self.addUnvisited(dist, head, state, headpath,
                                    [(formidx, link)], candidates, -1)
                        # the request associated to this link has never been
                        # made in the current state, add it as unvisited
                        if state not in nextabsreq.targets:
                            # XXX Possible Bug: Check why this exists
                            # do not pass form parameters, as this is an
                            # unvisited link and we want to pick random values
                            formidx = LinkIdx(idx.type, idx.path, None)
                            self.addUnvisited(dist, head, state, headpath,
                                    [(formidx, link)], candidates, 0)
                            continue
                        # do not put request in the heap, but just go for the next abstract page
                        tgt = nextabsreq.targets[state]
                        assert tgt.target
                        if (tgt.target, tgt.transition) in seen:
                            continue
                        formidx = LinkIdx(idx.type, idx.path, formparams)
                        newdist = dist + self.linkcost(head, formidx, link, state)
                        newpath = [PathStep(head, formidx, state)] + headpath                
                        heapq.heappush(heads, (newdist, tgt.target, tgt.transition, newpath))
                else:
                    if not unvlinks_added:
                        unvlinks = head.abslinks.getUnvisited(state)
                        if unvlinks:
                            self.addUnvisited(dist, head, state, headpath, unvlinks, candidates, 0, True)
                            unvlinks_added = True
                        else:
                            # TODO handle state changes
                            raise NotImplementedError
        # get the number of abstract pages reachable from here
        nvisited = len(set(i[0] for i in seen))
        if candidates:
            return candidates[0].path, nvisited
        else:
            return None, nvisited


    def getEngineAction(self, linkidx):
        if linkidx.type == Links.Type.ANCHOR:
            engineaction = Engine.Actions.ANCHOR
        elif linkidx.type == Links.Type.FORM:
            engineaction = Engine.Actions.FORM
        elif linkidx.type == Links.Type.REDIRECT:
            engineaction = Engine.Actions.REDIRECT
        else:
            assert False, linkidx
        return engineaction




    def getNextAction(self, reqresp):
        if self.pathtofollow:
            assert self.followingpath
            nexthop = self.pathtofollow.pop(0)
            if not reqresp.response.page.abspage.match(nexthop.abspage) or nexthop.state != self.state:
                self.followingpath = False
                self.pathtofollow = []
            else:
                assert nexthop.state == self.state
                if nexthop.idx is None:
                    assert not self.pathtofollow
                else:
                    # pass the index too, in case there are some form parameters specified
                    return (self.getEngineAction(nexthop.idx), reqresp.response.page.links[nexthop.idx], nexthop.idx)
        if self.followingpath and not self.pathtofollow:
            self.followingpath = False

        # This case only happens on the first page view
        if not reqresp.response.page.abspage:
            unvisited = self.getUnvisitedLink(reqresp)
            if unvisited:
                return (Engine.Actions.ANCHOR, reqresp.response.page.links[unvisited])
        else:
            # if there is only one REDIRECT, follow it
            abspage = reqresp.response.page.abspage
            linksiter = abspage.abslinks.iteritems()
            firstlink = None
            try:
                # extract the first link
                firstlink = linksiter.next()
                # next like will raise StopIteration if there is only 1 link
                linksiter.next()
            except StopIteration:
                # if we have at least one link, and it is the only one, and it is a redirect, 
                # then follow it
                if firstlink and firstlink[0].type == Links.Type.REDIRECT:
                    return (Engine.Actions.REDIRECT, reqresp.response.page.links[firstlink[0]])


            if self.ag and not self.keep_looking_for_links():
                # We aren't making progress and can't find any more links to visit
                return (Engine.Actions.DONE, )


            recentlyseen = set()
            rr = reqresp
            found = False
            while rr:
                destination = rr.response.page.abspage
                probablyseen = None
                for s, t in rr.request.absrequest.targets.iteritems():
                    if (t.target, t.transition) == (destination, self.state):
                        if s == self.state:
                            # state transition did not happen here
                            assert not probablyseen 
                            probablyseen = rr.request.absrequest
                        else:
                            # XXX we are matching only on target and not on
                            # source state, so it might detect that there was a
                            # state transition when there was none. it should
                            # not be an issue at this stage, but could it create
                            # an inifinite loop because it keep going to the
                            # "state-change revealing" pages?
                            found = True
                            break
                else:
                    # we should always be able to find the destination page in the target object
                    assert probablyseen
                    recentlyseen.add(probablyseen)
                if found:
                    break
                rr = rr.prev
            path, nvisited = self.findPathToUnvisited(reqresp, self.state, recentlyseen)
            if self.ag:
                # the running average history will be reset at envery discovery
                # of new abstract pages
                self.running_visited_avg.add(
                        float(nvisited)/len(self.ag.abspages),
                        len(self.ag.abspages))
            if path:
                # if there is a state change along the path, drop all following steps
                for i, p in enumerate(path):
                    if i > 0 and p.state != path[i-1].state:
                        path[i:] = []
                        break
                
                self.followingpath = True
                assert not self.pathtofollow
                self.pathtofollow = path
                nexthop = self.pathtofollow.pop(0)

                assert nexthop.abspage == reqresp.response.page.abspage
                assert nexthop.state == self.state
                # pass the index too, in case there are some form parameters specified
                return (self.getEngineAction(nexthop.idx), reqresp.response.page.links[nexthop.idx], nexthop.idx)
            else:
                self.logger.info("no new path found, but we have not explored"
                                 " enough")
                unv_anchors = []
                for idx, al in abspage.abslinks.iteritems():
                    if idx.type == Links.Type.ANCHOR:
                        assert self.state in al.targets
                        if al.targets[self.state].nvisits == 0:
                            unv_anchors.append(idx)
                if unv_anchors:
                    chosen = self.rng.choice(unv_anchors)
                    self.logger.info("picking unvisited anchor %s", chosen)
                    return (Engine.Actions.ANCHOR,
                            reqresp.response.page.links[chosen])

        self.logger.info("Couldn't find a path, so let's pick a random link!.")
        return (Engine.Actions.RANDOM, )

    def keep_looking_for_links(self):
        """
        This function returns true if we should continue looking for links, false otherwise
        """
        c = 4

        return (self.since_last_ar_change <= (c * self.last_ap_pages)) and (self.num_requests < 1800)

    def submitForm(self, form, params):
        if params is None:
            formkeys = form.elems
            submitparams = self.formfiller.getrandparams(formkeys, form)
            assert submitparams is not None
        else:
            submitparams = params
        return self.cr.submitForm(form, submitparams)

    def tryMergeInGraph(self, reqresp):
        if self.pc:
            self.logger.info(output.red("try to merge page into current state graph"))
            try:
                pc = self.pc
                ag = self.ag
                pc.addtolevelclustering(reqresp)
                ag.updatepageclusters(pc.getAbstractPages())
                newstate = ag.addtoAppGraph(reqresp, self.state)
                ag.fillMissingRequestsForPage(reqresp)
            except PageClusterer.AddToClusterException:
                self.logger.info(output.red("Level clustering changed, reclustering"))
                raise
            except PageClusterer.AddToAbstractPageException:
                self.logger.info(output.red("Unable to add page to current abstract page, reclustering"))
                raise
            except AppGraphGenerator.AddToAbstractRequestException:
                self.logger.info(output.red("Unable to add page to current abstract request, reclustering"))
                raise
            except AppGraphGenerator.AddToAppGraphException, e:
                reqresp.request.statehint = True
                self.logger.info(output.red("Unable to add page to current application graph, reclustering. %s" % e))
                raise
            except MergeLinksTreeException, e:
                self.logger.info(output.red("Unable to merge page into current linkstree, reclustering. %s" % e))
                raise
            except PageMergeException, e:
                raise RuntimeError("uncaught PageMergeException %s" % e)
        else:
            self.logger.info(output.red("first execution, start clustering"))
            raise PageMergeException()

        return newstate

    def main(self, urls, write_state_graph = False, write_ar_test = False, fuzz=True):
        
        ar_through_time = None
        if write_ar_test:
            ar_through_time = open('ar_test.dat', 'w')
        self.num_requests = 0
        self.last_ar_seen = -1
        self.since_last_ar_change = 0
        self.last_ap_pages = 1

        self.pc = None
        self.ag = None
        cr = Crawler(self.dumpdir)
        self.cr = cr
        global maxstate

        for cnt, url in enumerate(urls):

            cr.initial_url = url

            self.logger.info(output.purple("starting with URL %d/%d %s"), cnt+1, len(urls), url)
            maxstate = -1
            reqresp = cr.open(url)

            self.logger.info("Starting URL %s", reqresp)

            self.num_requests += 1

            statechangescores = None
            nextAction = self.getNextAction(reqresp)
            sinceclustered = 0
            while nextAction[0] != Engine.Actions.DONE:
                if nextAction[0] == Engine.Actions.ANCHOR:
                    reqresp = cr.click(nextAction[1])
                elif nextAction[0] == Engine.Actions.FORM:
                    try:
                        # pass form and parameters
                        reqresp = self.submitForm(nextAction[1], nextAction[2].params)
                    except Crawler.UnsubmittableForm:
                        nextAction[1].skip = True
                        nextAction = self.getNextAction(reqresp)
                elif nextAction[0] == Engine.Actions.REDIRECT:
                    reqresp = cr.followRedirect(nextAction[1])
                elif nextAction[0] == Engine.Actions.RANDOM:
                    randlink = self.rng.choice([i for i in reqresp.response.page.anchors])
                    reqresp = cr.click(randlink)
                else:
                    assert False, nextAction

                self.logger.info("%s", reqresp)
                try:
                    if nextAction[0] == Engine.Actions.FORM or sinceclustered > 15:
                         # do not even try to merge forms
                         raise PageMergeException()
                    self.state = self.tryMergeInGraph(reqresp)
                    maxstate += 1
                    self.logger.info(output.green("estimated current state %d (%d)"), self.state, maxstate)
                    sinceclustered += 1
                except PageMergeException:
                    self.logger.info("need to recompute graph")
                    sinceclustered = 0
                    statechangescores = self.cluster_everything(statechangescores)
                    # Assume that a form submission changed the state, until we recluster and discover otherwise
                    # if nextAction[0] == Engine.Actions.FORM:
                    #     self.state = maxstate


                self.num_requests += 1
                
                if write_ar_test:
                    ar_through_time.write("%d %d %d %d\n" % (self.num_requests, len(self.ag.absrequests), len([ar for ar in self.ag.absrequests if ar.request_actually_made()]), len(self.pc.getAbstractPages())))
                    ar_through_time.flush()

                ar_seen = len([ar for ar in self.ag.absrequests if ar.request_actually_made()])

                if ar_seen != self.last_ar_seen:
                    self.last_ar_seen = ar_seen
                    self.since_last_ar_change = 0
                else:
                    self.since_last_ar_change += 1
                
                self.last_ap_pages = len(self.pc.getAbstractPages())
                
                nextAction = self.getNextAction(reqresp)
                assert nextAction

                if wanttoexit:
                    return
                if write_state_graph and self.num_requests % 100 == 0:
                    self.writeStateDot()

        # cluster (for the last time)
        statechangescores = self.cluster_everything(statechangescores)
        self.writeStateDot()

        if not fuzz:
            return

        # Init the plugin_wrappers
        plugins = [plugin_wrapper(getattr(ap,name)) for ap, name in audit_plugins]

        self.fuzzing_web_client = htmlunit.WebClient()
        # new syntax for htmlunit 2.36
        fuzzing_options = htmlunit.WebClient.getOptions(self.fuzzing_web_client)

        self.fuzzing_web_client.setRefreshHandler(DeferringRefreshHandler())

        fuzzing_options.setThrowExceptionOnScriptError(False);

        fuzzing_options.setThrowExceptionOnFailingStatusCode(False);
        fuzzing_options.setUseInsecureSSL(True)
        fuzzing_options.setRedirectEnabled(False)
        fuzzing_options.setThrowExceptionOnFailingStatusCode(False);

        # optional addition to ignore JavaScript
        # fuzzing_options.setJavaScriptEnabled(False)

        # WE'RE GOING TO FUCKING TEST THE SHIT OUT OF THIS MEMORY LEAK
        # last_request = reqresp

        # while True:
        #     self.reset_until(last_request)


        # Go through the requests and uniquely fuzz them
        self.reset_web_application()
        requests_states_already_fuzzed = set()
        try:
            cur_reqresp = self.cr.headreqresp
            while cur_reqresp:
                cur_state = cur_reqresp.request.reducedstate
                if not (cur_reqresp.request.signature_vector, cur_state) in requests_states_already_fuzzed:
                    need_to_check_state = self.is_state_changing_reqresp(cur_reqresp)
                    print "FUZZING", (cur_reqresp.request.signature_vector, cur_state)
                    for plugin in plugins:
                        try:
                            p = plugin.audit(cur_reqresp.request)
                            new_req = p.next()
                            
                            while True:
                                response = self.send_fuzzing_request(new_req)
                                
                                if need_to_check_state:
                                    state_revealing_reqresp = self.get_state_revealing_reqresp(cur_reqresp)
                                    if state_revealing_reqresp:
                                        state_revealing_response = self.send_fuzzing_request(state_revealing_reqresp.request)
                                        same_page_clusters = self.pc.classif.get_object(state_revealing_response.page.reqresp)
                                        if same_page_clusters:
                                            states_maps_to = set(reqresp.response.page.reducedstate for reqresp in same_page_clusters)
                                            if cur_state in states_maps_to:
                                                print "haven't changed state, we're OK"
                                                pass
                                            else:
                                                print "we accidentially changed the state while fuzzing"
                                                # We changed state with the last request, time to reset
                                                # Try to find a request to bring us back
                                                request_to_change_back = self.get_request_to_change_state(cur_reqresp, cur_state)
                                                if request_to_change_back:
                                                    # make the request
                                                    self.send_fuzzing_request(request_to_change_back)

                                                    # check the state revealing page                                         
                                                    state_revealing_response = self.send_fuzzing_request(state_revealing_reqresp.request)
                                                    same_page_clusters = self.pc.classif.get_object(state_revealing_response.page.reqresp)
                                                    if same_page_clusters:
                                                        states_maps_to = set(reqresp.response.page.reducedstate for reqresp in same_page_clusters)
                                                        if cur_state in states_maps_to:
                                                            print "Back in the correct state!"
                                                            pass
                                                        else:
                                                            print "request_to_change_back didn't take us back. Reset everything"
                                                            self.reset_until(cur_reqresp)
                                                    else:
                                                        print "After sending the changing state the state revealing page is brand new"
                                                        self.reset_until(cur_reqresp)
                                                else:
                                                    print "No request to put us back in the state, reset"
                                                    self.reset_until(cur_reqresp)
                                                    
                                        else:
                                            print "We're in a strange place, the request didn't go anywhere"
                                            self.reset_until(cur_reqresp)
                                    else:
                                        need_to_check_state = False

                                # need to check that the new_req is on the response page
                                
                                new_req = p.send(response)
                                
                        except StopIteration:
                            pass
                else:
                    print "SKIPPING", requests_states_already_fuzzed.add((cur_reqresp.request.signature_vector, cur_state))

                # Make the actual request
                self.send_fuzzing_request(cur_reqresp.request)
                cur_reqresp = cur_reqresp.next

        finally:
            for plugin in plugins:
                plugin.stop()

    def get_request_to_change_state(self, reqresp, target_state):
        cur_state = reqresp.response.page.reducedstate
        reqresp = reqresp.next
        toReturn = None
        while reqresp:
            if reqresp.request.reducedstate == cur_state and reqresp.response.page.reducedstate == target_state:
                toReturn = reqresp.request
                break
            reqresp = reqresp.next


        return toReturn

    def reset_until(self, reqresp):

        # new method for htnmlunit 2.36
        self.fuzzing_web_client.close()

        self.fuzzing_web_client = htmlunit.WebClient()
        # new syntax for htmlunit 2.36
        fuzzing_options = htmlunit.WebClient.getOptions(self.fuzzing_web_client)

        self.fuzzing_web_client.setRefreshHandler(DeferringRefreshHandler())

        fuzzing_options.setThrowExceptionOnScriptError(False);

        fuzzing_options.setThrowExceptionOnFailingStatusCode(False);
        fuzzing_options.setUseInsecureSSL(True)
        fuzzing_options.setRedirectEnabled(False)
        fuzzing_options.setThrowExceptionOnFailingStatusCode(False);

        # optional addition to ignore JavaScript
        # fuzzing_options.setJavaScriptEnabled(False)

        self.reset_web_application()
        cur_reqresp = self.cr.headreqresp
        while cur_reqresp != reqresp:
            self.send_fuzzing_request(cur_reqresp.request)
            cur_reqresp = cur_reqresp.next

    def get_state_revealing_reqresp(self, reqresp):
        toReturn = None
        while reqresp:
            if reqresp.request.statehint:
                toReturn = reqresp
                break
            reqresp = reqresp.next
        return toReturn

    def is_state_changing_reqresp(self, reqresp):
        return reqresp.request.changingstate

    def reset_web_application(self):
        print "resetting the web application"
        if self.command_to_reset:
            result = self.run_command(self.command_to_reset)
            print result[0]
            print result[1]

    def run_command(self, command):
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True).communicate()

    def send_fuzzing_request(self, new_req):
        # Make the request and return a new response
        start_time = time.time()
        generic_page = self.fuzzing_web_client.getPage(new_req.webrequest)
        htmlpage = None
        try:
            htmlpage = generic_page
        except:
            pass
        if isinstance(htmlpage, htmlunit.html.HtmlPage):
            page = Page(htmlpage, initial_url=self.cr.initial_url, webclient=self.fuzzing_web_client)
        else:
            page = Page(generic_page.getWebResponse(), error=True, initial_url=self.cr.initial_url, webclient=self.fuzzing_web_client)
            
        webresponse = generic_page.getWebResponse()

        response = Response(webresponse, page=page)
        response.time = time.time()-start_time
        reqresp = RequestResponse(new_req, response)

        new_req.reqresp = reqresp
        page.reqresp = reqresp

        return response

    def cluster_everything(self, statechangescores):
        pc = PageClusterer(self.cr.headreqresp)
        ag = AppGraphGenerator(self.cr.headreqresp, pc.getAbstractPages(),
                               statechangescores, self.formfiller, self.cr)
        global maxstate
        maxstate = ag.generateAppGraph()
        self.state = ag.reduceStates()
        ag.markChangingState()
        def check(x):
            l = list(x)
            v = reduce(lambda a, b: (a[0] + b[0], (a[1] + b[1])), l, (0, 0))
            v = (v[0], v[1]/2.0)
            return v
        statechangescores = RecursiveDict(nleavesfunc=lambda x: x, nleavesaggregator=check)
        rr = self.cr.headreqresp
        while rr:
            changing = 1 if rr.request.reducedstate != rr.response.page.reducedstate else 0
            statechangescores.setapplypathvalue([rr.request.method] + list(rr.request.urlvector),
                                                (1, changing), lambda x: (x[0] + 1, x[1] + changing))
            rr.request.state = -1
            rr.response.page.state = -1
            rr = rr.next
            
        ag.fillMissingRequests()
            
        self.pc = pc
        self.ag = ag
        return statechangescores



    def writeDot(self):
        if not self.ag:
            self.logger.info("not creating DOT graph")
            return

        self.logger.info("creating DOT graph")
        dot = pydot.Dot()
        nodes = {}

        for p in self.ag.abspages:
            name = p.label
            node = pydot.Node(name)
            nodes[p] = node

        for p in self.ag.allabsrequests:
            name = str('\\n'.join(p.requestset))
            node = pydot.Node(name)
            nodes[p] = node

        for n in nodes.itervalues():
            dot.add_node(n)

        for p in self.ag.abspages:
            for l in p.abslinks:
                linksequal = defaultdict(list)
                for s, t in l.targets.iteritems():
                    assert s == t.transition
                    tgt = t.target
                    if not isinstance(tgt, FormTarget.Dict):
                        linksequal[t.target].append(s)
                for t, ss in linksequal.iteritems():
                    try:
                        edge = pydot.Edge(nodes[p], nodes[t])
                    except KeyError, e:
                        self.logger.warn("Cannot find node %s", e.args[0])
                        continue
                    ss.sort()
                    edge.set_label(",".join(str(i) for i in ss))
                    dot.add_edge(edge)

        for p in self.ag.absrequests:
            linksequal = defaultdict(list)
            for s, t in p.targets.iteritems():
                if s != t.transition:
                    edge = pydot.Edge(nodes[p], nodes[t.target])
                    edge.set_label("%s->%s" % (s, t.transition))
                    edge.set_color("red")
                    dot.add_edge(edge)
                else:
                    linksequal[t.target].append(s)
            for t, ss in linksequal.iteritems():
                try:
                    edge = pydot.Edge(nodes[p], nodes[t])
                except KeyError, e:
                    self.logger.warn("Cannot find node %s", e.args[0])
                    continue
                ss.sort()
                edge.set_label(",".join(str(i) for i in ss))
                dot.add_edge(edge)
                edge.set_color("blue")

        dot.write_ps('graph.ps')
        f = open('graph.dot', 'w')
        f.write(dot.to_string())
        f.close()

    def writeStateDot(self):
        if not self.ag:
            self.logger.info("not creating state DOT graph")
            return

        self.logger.info("creating state DOT graph")

        nodes = {}
        
        for p in self.ag.allabsrequests:
            for s, t in p.targets.iteritems():
                if s != t.transition:
                    requestset = list(p.requestset)
                    if len(requestset) > 5:
                        requestset = requestset[:5]
                        
                    name = str('\\n'.join(requestset))

                    initial_state = str(s)
                    transition = str(t.transition)
                    nodes[(initial_state, transition)] = name

        f = open('stategraph.dot', 'w')
        f.write("digraph G {\n")

        for (states, request_names) in nodes.iteritems():
            initial_state = states[0]
            transition = states[1]
            f.write(initial_state)
            f.write(" -> ")
            f.write(transition)
            f.write(" [label=")
            f.write(quote_if_necessary(request_names))
            f.write("];\n")
            
        f.write("}\n")

        f.close()

dot_keywords = ['graph', 'subgraph', 'digraph', 'node', 'edge', 'strict']

id_re_alpha_nums = re.compile('^[_a-zA-Z][a-zA-Z0-9_:,]*$')
id_re_num = re.compile('^[0-9,]+$')
id_re_with_port = re.compile('^([^:]*):([^:]*)$')
id_re_dbl_quoted = re.compile('^\".*\"$', re.S)
id_re_html = re.compile('^<.*>$', re.S)


def needs_quotes( s ):
    """Checks whether a string is a dot language ID.
    
    It will check whether the string is solely composed
    by the characters allowed in an ID or not.
    If the string is one of the reserved keywords it will
    need quotes too.
    """
        
    if s in dot_keywords:
        return False

    chars = [ord(c) for c in s if ord(c)>0x7f or ord(c)==0]
    if chars and not id_re_dbl_quoted.match(s):
        return True
        
    for test in [id_re_alpha_nums, id_re_num, id_re_dbl_quoted, id_re_html]:
        if test.match(s):
            return False

    m = id_re_with_port.match(s)
    if m:
        return needs_quotes(m.group(1)) or needs_quotes(m.group(2))

    return True


def quote_if_necessary(s):

    if isinstance(s, bool):
        if s is True:
            return 'True'
        return 'False'

    if not isinstance( s, basestring ):
        return s

    if needs_quotes(s):
        replace = {'"'  : r'\"',
                   "\n" : r'\n',
                   "\r" : r'\r'}
        for (a,b) in replace.items():
            s = s.replace(a, b)

        return '"' + s + '"'
     
    return s   



def writeColorableStateGraph(allstates, differentpairs):

    dot = pydot.Dot(graph_type='graph')
    nodes = {}
    for s in allstates:
        node = pydot.Node(str(s))
        nodes[s] = node
        dot.add_node(node)


    for s, t in differentpairs:
        edge = pydot.Edge(nodes[s], nodes[t])
        dot.add_edge(edge)

    dot.write_ps('colorablestategraph.ps')
    f = open('colorablestategraph.dot', 'w')
    f.write(dot.to_string())
    f.close()

if __name__ == "__main__":
    optslist, args = getopt.getopt(sys.argv[1:], "l:d:R:DsaF")
    opts = dict(optslist) if optslist else {}

    level = logging.INFO
    if opts.has_key('-D'):
        level = logging.DEBUG
    try:
        handler = logging.FileHandler(opts['-l'], 'w')
        logging.basicConfig(level=level, filename=opts['-l'],
                filemode='w')
    except KeyError:
        logging.basicConfig(level=level)

    command_to_reset = None
    if opts.has_key('-R'):
        command_to_reset = opts['-R']

    print "COMMAND", command_to_reset

    write_ar_test = opts.has_key('-a')
    write_state_graph = opts.has_key('-s')

    fuzz = not opts.has_key('-F')

    dumpdir = None
    try:
        dumpdir = opts['-d']
        # where to dump HTTP requests and responses
        os.makedirs(dumpdir)
    except KeyError:
        pass
    except OSError, e:
        if e.errno != errno.EEXIST:
            raise
        num_re = re.compile(r"\d+")
        for d in os.listdir(dumpdir):
            if num_re.match(d):
                shutil.rmtree("%s/%s" % (dumpdir, d))

    ff = FormFiller()
    login = FormFiller.Params({'username': ['scanner1'], 'password': ['scanner1']})
    ff.add(login)
    login = FormFiller.Params({'username': ['scanner1'], 'password': ['scanner1'], 'autologin': ['off'], 'login': ['Log in']})
    ff.add(login)
    login = FormFiller.Params({'adminname': ['admin'], 'password': ['admin']})
    ff.add(login)
    login = FormFiller.Params({'email': ['scanner1'], 'password': ['scanner1']})
    ff.add(login)
    login = FormFiller.Params({'User/Email': ['scanner1'], 'User/Password': ['scanner1'], 'User/Sign_In': ['Sign In'], 'User/RememberMe': [0]})
    ff.add(login)
    ff.add_named_params(["email", "mail", "User/Email"], "adoupe@cs.ucsb.edu")
    ff.add_named_params(["url"], "http://example.com/")
    ff.add_named_params(["hpt"], "")
    e = Engine(ff, dumpdir, command_to_reset)
    try:
        e.main(args, write_state_graph, write_ar_test, fuzz)
    except:
        traceback.print_exc()
        if level == logging.DEBUG:
            pdb.post_mortem()
    finally:
        pass
    print kb.kb.getAllVulns()


# vim:sw=4:et:
